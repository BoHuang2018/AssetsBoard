"""IBKR Flex Query import / analysis / Flex Web Service download (offline, synthetic data)."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assetsboard import ibkr, ibkr_flex as F

XML = """<FlexQueryResponse queryName="t" type="AF"><FlexStatements count="3">
<FlexStatement accountId="U1" fromDate="20260101" toDate="20260101" period="YearToDate" whenGenerated="20261004;135228">
 <ChangeInNAV accountId="U1" currency="USD" fromDate="20260101" toDate="20260101" startingValue="0" endingValue="0" twr="0"/>
 <Trades/><CashTransactions/>
</FlexStatement>
<FlexStatement accountId="U1" fromDate="20260102" toDate="20260102" period="YearToDate" whenGenerated="20261004;135228">
 <ChangeInNAV accountId="U1" currency="USD" fromDate="20260102" toDate="20260102" startingValue="0" depositsWithdrawals="1000" commissions="-3" mtm="10" endingValue="1007" twr="0.7"/>
 <Trades>
  <Trade accountId="U1" currency="NOK" fxRateToBase="0.1" assetCategory="CASH" symbol="USD.NOK" tradeID="T1" dateTime="20260102;100000" tradeDate="20260102" quantity="100" tradePrice="10.01" tradeMoney="1001" ibCommission="-2" ibCommissionCurrency="USD" buySell="BUY" notes="" levelOfDetail="EXECUTION"/>
  <Trade accountId="U1" currency="USD" fxRateToBase="1" assetCategory="STK" symbol="AAA" tradeID="T2" dateTime="20260102;103000" tradeDate="20260102" quantity="2" tradePrice="50" tradeMoney="100" ibCommission="-1" ibCommissionCurrency="USD" fifoPnlRealized="0" buySell="BUY" levelOfDetail="EXECUTION"/>
  <Trade accountId="U1" currency="USD" fxRateToBase="1" assetCategory="STK" symbol="AAA" tradeID="T2" dateTime="20260102;103000" levelOfDetail="CLOSED_LOT"/>
 </Trades>
 <UnbundledCommissionDetails>
  <UnbundledCommissionDetail assetCategory="STK" tradeID="T2" dateTime="20260102;103000" quantity="2" price="50" fxRateToBase="1" brokerExecutionCharge="-0.99" thirdPartyRegulatoryCharge="-0.01" regSection31TransactionFee="-0.01" totalCommission="-1"/>
 </UnbundledCommissionDetails>
 <CashTransactions>
  <CashTransaction currency="NOK" fxRateToBase="0.1" type="Deposits/Withdrawals" amount="10000" dateTime="20260102" transactionID="C1" levelOfDetail="DETAIL" description="CASH RECEIPTS"/>
 </CashTransactions>
</FlexStatement>
<FlexStatement accountId="U1" fromDate="20260103" toDate="20260103" period="YearToDate" whenGenerated="20261004;135228">
 <ChangeInNAV accountId="U1" currency="USD" fromDate="20260103" toDate="20260103" startingValue="1007" dividends="1" withholdingTax="-0.15" interest="0.5" otherFees="-0.05" mtm="5" commissions="-1" endingValue="1012.3" twr="0.5"/>
 <Trades>
  <Trade accountId="U1" currency="USD" fxRateToBase="1" assetCategory="STK" symbol="AAA" tradeID="T3" dateTime="20260103;103000" tradeDate="20260103" quantity="-1" tradePrice="60" tradeMoney="-60" ibCommission="-1" ibCommissionCurrency="USD" fifoPnlRealized="8" buySell="SELL" levelOfDetail="EXECUTION"/>
 </Trades>
 <CashTransactions>
  <CashTransaction currency="USD" fxRateToBase="1" type="Dividends" symbol="AAA" amount="1" dateTime="20260103" transactionID="C2" description="AAA CASH DIVIDEND"/>
  <CashTransaction currency="USD" fxRateToBase="1" type="Withholding Tax" symbol="AAA" amount="-0.15" dateTime="20260103" transactionID="C3" description="AAA US TAX"/>
  <CashTransaction currency="USD" fxRateToBase="1" type="Broker Interest Received" amount="0.5" dateTime="20260103" transactionID="C4" description="SYEP INTEREST"/>
  <CashTransaction currency="USD" fxRateToBase="1" type="Other Fees" symbol="AAA" amount="-0.05" dateTime="20260103;202000" transactionID="C5" description="AAA ADR Fee USD 0.02 PER SHARE - FEE"/>
  <CashTransaction currency="USD" fxRateToBase="1" type="Dividends" amount="9" levelOfDetail="SUMMARY"/>
 </CashTransactions>
</FlexStatement>
</FlexStatements></FlexQueryResponse>"""


class FlexImport(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.p = Path(self.td.name)
        self.xml = self.p / "s.xml"
        self.xml.write_text(XML, encoding="utf-8")
        self.root = self.p / "archive" / "ibkr"
        (self.p / "snapshots").mkdir()
        (self.p / "snapshots" / "ibkr_fx_refs.json").write_text(json.dumps({"T1": {"ref": 10.0, "src": "test"}}))

    def tearDown(self):
        self.td.cleanup()

    def test_import_dedup_and_analysis(self):
        r1 = F.import_xml(self.xml, self.root)
        self.assertEqual(r1["sections"]["trades"]["new"], 3)          # CLOSED_LOT row skipped
        self.assertEqual(r1["sections"]["cash"]["new"], 5)            # SUMMARY row skipped
        r2 = F.import_xml(self.xml, self.root)
        self.assertTrue(all(v["new"] == 0 and v["updated"] == 0 for v in r2["sections"].values()))
        a = F.analyze(self.p, self.root, offline=True)
        self.assertTrue(a["complete"])
        self.assertEqual(a["account_start"], "20260102")
        self.assertAlmostEqual(a["net_inflow"], 1000)
        self.assertAlmostEqual(a["pnl"], 12.3)
        self.assertAlmostEqual(a["realized"]["total"], 8)
        self.assertEqual(a["realized"]["sells"], 1)
        self.assertAlmostEqual(a["dividends"]["gross"], 1)
        self.assertAlmostEqual(a["dividends"]["net"], 0.85)
        self.assertAlmostEqual(a["interest_total"], 0.5)
        self.assertAlmostEqual(a["costs"]["stock_commission"], 2)
        self.assertAlmostEqual(a["costs"]["fx_commission"], 2)
        self.assertAlmostEqual(a["costs"]["fx_spread_est"], 100 * 0.01 * 0.1)   # 0.1% on 1001 NOK = 0.1 USD
        self.assertAlmostEqual(a["fx"]["vw_markup_pct"], 0.1, places=3)
        self.assertAlmostEqual(a["costs"]["other_fees"], 0.05)
        self.assertEqual(a["fee_kinds"], {"ADR 託管費": -0.05})
        self.assertAlmostEqual(a["commission_breakdown"]["broker"], 0.99)
        self.assertAlmostEqual(a["commission_breakdown"]["regulatory"], 0.01)
        self.assertAlmostEqual(a["ratios"]["stock_commission_pct_of_stock_volume"], 2 / 160 * 100)

    def test_incomplete_history_is_hybrid(self):
        xml = XML.replace('startingValue="0" depositsWithdrawals="1000"', 'startingValue="500" depositsWithdrawals="1000"').replace(
            '<ChangeInNAV accountId="U1" currency="USD" fromDate="20260101" toDate="20260101" startingValue="0" endingValue="0" twr="0"/>', "")
        self.xml.write_text(xml, encoding="utf-8")
        F.import_xml(self.xml, self.root)
        a = F.analyze(self.p, self.root, offline=True)
        self.assertFalse(a["complete"])
        self.assertAlmostEqual(a["opening_value"], 500)
        self.assertAlmostEqual(a["pnl"], 1012.3 - 500 - 1000)

    def test_flex_replaces_baseline_in_ibkr_analyze(self):
        F.import_xml(self.xml, self.root)
        t = ibkr.now_oslo()
        snap = {"exchange": "ibkr", "captured_oslo": t.isoformat(timespec="seconds"), "account": "U1", "base_currency": "USD",
                "net_liquidation": 1020.0, "total_cash": 20.0, "positions": [], "pnl": None}
        ibkr.write_snapshot(self.root / t.strftime("%Y-%m") / "snapshots", snap, t)
        ibkr.save_baseline(self.root, snap, 1.0)
        with mock.patch.object(ibkr, "available", return_value=False):
            a = ibkr.analyze(self.p, self.root, offline=True)
        self.assertEqual(a["pnl_method"], "flex")
        self.assertAlmostEqual(a["net_inflow_base"], 1000)
        self.assertAlmostEqual(a["pnl_base"], 20)

    def test_not_flex(self):
        bad = self.p / "x.xml"
        bad.write_text("<a><b/></a>")
        with self.assertRaises(F.FlexError):
            F.import_xml(bad, self.root)


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FlexWebService(unittest.TestCase):
    def test_fetch_polls_until_ready(self):
        calls = []
        replies = [b"<FlexStatementResponse><Status>Success</Status><ReferenceCode>R9</ReferenceCode><Url>https://evil.example.com/x</Url></FlexStatementResponse>",
                   b"<FlexStatementResponse><Status>Warn</Status><ErrorCode>1019</ErrorCode><ErrorMessage>in progress</ErrorMessage></FlexStatementResponse>",
                   XML.encode()]

        def opener(req, timeout=0):
            calls.append(req)
            return FakeResp(replies[len(calls) - 1])
        with tempfile.TemporaryDirectory() as td:
            p = F.flex_fetch("SECRET", "123", Path(td), opener=opener, sleep=lambda s: None)
            self.assertTrue(p.read_bytes().startswith(b"<FlexQueryResponse"))
        self.assertIn("/FlexWebService/SendRequest?t=SECRET&q=123&v=3", calls[0].full_url)
        self.assertTrue(calls[1].full_url.startswith(F.FLEX_BASE + "/GetStatement?"))     # foreign Url ignored
        self.assertIn("q=R9", calls[1].full_url)
        self.assertTrue(all(c.get_header("User-agent") for c in calls))

    def test_unresolvable_getstatement_host_falls_back(self):
        calls = []

        def opener(req, timeout=0):
            calls.append(req.full_url)
            if len(calls) == 1:
                return FakeResp(b"<FlexStatementResponse><Status>Success</Status><ReferenceCode>R1</ReferenceCode><Url>https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement</Url></FlexStatementResponse>")
            if "gdcdyn" in req.full_url:
                raise OSError("dns")
            return FakeResp(XML.encode())
        with tempfile.TemporaryDirectory() as td:
            F.flex_fetch("SECRET", "1", Path(td), opener=opener, sleep=lambda s: None)
        self.assertIn("gdcdyn", calls[1])
        self.assertTrue(calls[2].startswith(F.FLEX_BASE + "/GetStatement?"))

    def test_errors_do_not_leak_token(self):
        def opener(req, timeout=0):
            return FakeResp(b"<FlexStatementResponse><Status>Fail</Status><ErrorCode>1012</ErrorCode><ErrorMessage>Token has expired.</ErrorMessage></FlexStatementResponse>")
        with self.assertRaises(F.FlexError) as cm:
            F.flex_fetch("SECRET", "123", Path("/nonexistent"), opener=opener, sleep=lambda s: None)
        self.assertIn("1012", str(cm.exception))
        self.assertNotIn("SECRET", str(cm.exception))

        def boom(req, timeout=0):
            e = OSError("x"); e.reason = "bad thing for " + req.full_url + " SECRET"; raise e
        with self.assertRaises(F.FlexError) as cm:
            F.flex_fetch("SECRET", "123", Path("/nonexistent"), opener=boom, sleep=lambda s: None)
        self.assertNotIn("SECRET", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
