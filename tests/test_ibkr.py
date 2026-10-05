"""Offline tests for assetsboard.ibkr against a fake local IB Gateway (no real Gateway, no network)."""
import json
import os
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from assetsboard import ibkr


def frame(*fields):
    b = ("\0".join(str(f) for f in fields) + "\0").encode()
    return struct.pack(">I", len(b)) + b


class FakeGateway(threading.Thread):
    """Speaks the TWS handshake + replies for the read requests assetsboard sends. Records received message ids."""

    def __init__(self, mode="ok"):
        super().__init__(daemon=True)
        self.srv = socket.socket(); self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0)); self.srv.listen(1)
        self.port, self.mode, self.received, self.handshake = self.srv.getsockname()[1], mode, [], None

    def run(self):
        conn, _ = self.srv.accept()
        buf = b""

        def read():
            nonlocal buf
            while True:
                if len(buf) >= 4:
                    n = struct.unpack(">I", buf[:4])[0]
                    if len(buf) >= 4 + n:
                        m, buf = buf[4:4 + n], buf[4 + n:]
                        return m
                c = conn.recv(65536)
                if not c:
                    return None
                buf += c
        hdr = conn.recv(4)
        assert hdr == b"API\0"
        self.handshake = read().decode()
        if self.mode == "silent":
            conn.close(); return
        conn.sendall(frame(176, "20261004 18:00:00 CET"))
        while True:
            m = read()
            if m is None:
                break
            f = m.decode().split("\0")[:-1]
            mid = int(f[0]); self.received.append(mid)
            if mid == 71:
                if self.mode == "busy":
                    conn.sendall(frame(4, 2, -1, 326, "Unable to connect as the client id is already in use.")); continue
                conn.sendall(frame(15, 1, "U25459509") + frame(9, 1, 1) + frame(4, 2, -1, 2104, "Market data farm connection is OK:usfarm"))
            elif mid == 62:
                rows = [("NetLiquidation", "100000.50", "NOK"), ("TotalCashValue", "20000.25", "NOK"), ("GrossPositionValue", "80000.25", "NOK"),
                        ("AccountType", "INDIVIDUAL", "")]
                conn.sendall(b"".join(frame(63, 1, f[2], "U25459509", t, v, c) for t, v, c in rows) + frame(64, 1, f[2]))
            elif mid == 61:
                conn.sendall(frame(61, 3, "U25459509", 265598, "AAPL", "STK", "", 0, "", "", "NASDAQ", "USD", "AAPL", "NMS", "10", "150.5")
                             + frame(62, 1))
            elif mid == 6 and f[2] == "1":
                vals = [("$LEDGER-ExchangeRate", "10.5", "USD"), ("ExchangeRate", "1.00", "NOK"), ("CashBalance", "20000.25", "NOK"),
                        ("CashBalance", "0", "USD"), ("CashBalance", "20000.25", "BASE"), ("RealizedPnL", "123.4", "BASE"),
                        ("UnrealizedPnL", "7600", "BASE")]
                conn.sendall(b"".join(frame(6, 2, k, v, c, "U25459509") for k, v, c in vals)
                             + frame(7, 8, 265598, "AAPL", "STK", "", 0, "", "", "NASDAQ", "USD", "AAPL", "NMS", "10", "222.0",
                                     "2220.0", "150.5", "715.0", "0", "U25459509")
                             + frame(8, 1, "18:00") + frame(54, 1, "U25459509"))
            elif mid == 92:
                conn.sendall(frame(94, f[1], "-12.5", "7600", "1.7976931348623157E308"))
            elif mid == 7:
                conn.sendall(frame(11, f[2], 5, 265598, "AAPL", "STK", "", 0, "", "", "NASDAQ", "USD", "AAPL", "NMS",
                                   "0000e0d5.66fb1c4b.01.01", "20261002  15:30:01 US/Eastern", "U25459509", "NASDAQ", "BOT", "10",
                                   "150.5", 123, 0, 0, "10", "150.5", "", "", "", "", 1)
                             + frame(55, 1, f[2]) + frame(59, 1, "0000e0d5.66fb1c4b.01.01", "1.0", "USD", "1.7976931348623157E308", "", ""))
        conn.close()


class Protocol(unittest.TestCase):
    def test_write_ids_refused_before_send(self):
        c = ibkr.TwsReader(port_=1)
        c.sock = mock.Mock()
        for mid in (3, 4, 58, 21, 26, 69, 8, 999):
            with self.assertRaises(ibkr.WriteRefused):
                c.send(mid, 1, 2)
        c.sock.sendall.assert_not_called()

    def test_only_localhost(self):
        with self.assertRaises(ValueError):
            ibkr.TwsReader(host="10.0.0.5")

    def test_encode(self):
        self.assertEqual(ibkr.encode((6, 2, True, "U1")), struct.pack(">I", 9) + b"6\x002\x001\x00U1\x00")

    def test_offline_gateway(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
        with mock.patch.dict(os.environ, {"IBKR_PORT": str(p)}):
            self.assertFalse(ibkr.available())
            with self.assertRaises(ibkr.IbkrUnavailable) as cm:
                ibkr.fetch()
            self.assertIn("IB Gateway", str(cm.exception))

    def test_silent_and_busy(self):
        for mode, word in (("silent", "關閉了連線"), ("busy", "clientId")):
            g = FakeGateway(mode); g.start()
            with mock.patch.dict(os.environ, {"IBKR_PORT": str(g.port)}):
                with self.assertRaises(ibkr.IbkrUnavailable) as cm:
                    ibkr.fetch()
            self.assertIn(word, str(cm.exception))


class Session(unittest.TestCase):
    def test_full_session_and_archive(self):
        g = FakeGateway(); g.start()
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"IBKR_PORT": str(g.port), "IBKR_CLIENT_ID": "47"}), \
                mock.patch.object(ibkr, "usd_per", return_value=(1 / 10.5, "test")), mock.patch.object(ibkr, "usdt_per_usd", return_value=1.0):
            root = Path(td) / "archive" / "ibkr"
            rep = ibkr.archive_run(root, progress=False)
            g.join(3)
            self.assertEqual(g.handshake, "v157..178")
            self.assertTrue(set(g.received) <= set(ibkr.OUT_ALLOWED), g.received)
            self.assertNotIn(3, g.received); self.assertNotIn(4, g.received)
            self.assertEqual(rep["errors"], {})
            self.assertEqual(rep["sources"]["ibkr_executions"]["added"], 1)
            snap, _ = ibkr.latest_snapshot([root])
            self.assertEqual(snap["base_currency"], "NOK")
            self.assertAlmostEqual(snap["net_liquidation"], 100000.50)
            p = snap["positions"][0]
            self.assertEqual((p["symbol"], p["position"], p["avg_cost"]), ("AAPL", 10, 150.5))
            self.assertAlmostEqual(p["market_value_base"], 2220.0 * 10.5)
            self.assertEqual(snap["cash_by_currency"], {"NOK": 20000.25})
            self.assertIsNone(snap["pnl"]["realized"])                 # UNSET double -> None
            ex = ibkr.load_executions(root)[0]
            self.assertEqual((ex["side"], ex["commission"]), ("BOT", 1.0))
            self.assertEqual(ex["_ms"], 1790969401000)
            b = ibkr.load_baseline(root)
            self.assertAlmostEqual(b["value_base"], 100000.50)
            a = ibkr.analyze(Path(td), root, offline=True)
            self.assertTrue(a["present"])
            self.assertAlmostEqual(a["cash_share"], 20000.25 / 100000.50 * 100)
            self.assertAlmostEqual(a["total"], 100000.50 / 10.5)
            self.assertAlmostEqual(a["pnl_base"], 0.0)


if __name__ == "__main__":
    unittest.main()
