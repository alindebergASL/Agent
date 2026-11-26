import io
from pathlib import Path
import tempfile
import unittest

from docx import Document
from openpyxl import Workbook
from fastapi.testclient import TestClient
from work_agent.storage import Store
from work_agent.web import create_app
from work_agent.calculation_evidence import calculate_with_evidence


def packed(document):
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


def budget(rate):
    book = Workbook()
    book.active.title = "Demand"
    book.active.append(["Units", 10])
    rates = book.create_sheet("Rates")
    rates.append(["Rate", rate])
    rates.append(["Total", "=Demand!B1*B1"])
    return packed(book)


def word_policy():
    doc = Document()
    doc.add_paragraph("The standard review is quarterly.")
    doc.add_table(rows=1, cols=1).cell(0, 0).text = "Escorted visits only."
    return packed(doc)


class OfficeEvidenceTests(unittest.TestCase):
    def test_original_and_revised_decisions_remain_linked_to_exact_cells(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = Store(root)
        store.initialize()
        cap = 100
        reports = []
        for rate in (8, 12):
            raw = budget(rate)
            source = store.submit("Archive this workbook", {"budget.xlsx": raw})
            store.claim()
            store.complete(source, "Archived")
            task = store.submit("Decide if we can fund the order", {}, prior_revision=(source,"budget.xlsx",None))
            store.claim()
            pin = store.get_task(task)["prior_artifacts"][0]["reference_id"]
            lines = store.read_prior_text(task, pin).decode().splitlines()
            receipts = {}
            args = []
            for idx, address, variable in [(2,"'Demand'!B1","units"), (4,"'Rates'!B1","rate")]:
                coordinate, label, value = lines[idx-1].split("\t")
                self.assertEqual((coordinate,label),(address,"number"))
                url = f"/tasks/{source}/uploads/budget.xlsx#L{idx}"
                receipts[url] = ("budget.xlsx",lines[idx-1])
                args.append(dict(name=variable,value=value,source_url=url,interpretation=variable))
            result = calculate_with_evidence("units*rate",args,receipts,lambda _,v:v["units"]*v["rate"])
            decision = "Fund" if result["result"] <= cap else "Hold"
            report = decision + " " + " ".join(f"[{a['name']}]({a['source_url']})" for a in args)
            store.stage(task,"decision.md",report)
            store.complete(task,decision)
            reports.append((task,source,decision,raw))
            self.assertTrue(store.search_saved_work(task,"budget.xlsx","upload"))
        self.assertEqual([r[2] for r in reports],["Fund","Hold"])
        with TestClient(create_app(root)) as client:
            for task,source,decision,raw in reports:
                page = client.get(f"/tasks/{task}/artifacts/decision.md?revision=1")
                self.assertIn(decision,page.text)
                self.assertIn(f"/tasks/{source}/uploads/budget.xlsx#L4",page.text)
                link = f"/tasks/{source}/uploads/budget.xlsx"
                self.assertIn('id="L4"',client.get(link).text)
                self.assertEqual(client.get(link+"?download=1").content,raw)

    def test_word_body_and_table_share_normal_upload_and_evidence_view(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        raw = word_policy()
        from tests.quote_fixture import answer
        class Done:
            def respond(self,*args,**kwargs):
                return {"status":"completed","output":[answer("Archived")]}
        with TestClient(create_app(root,Done())) as client:
            response=client.post("/tasks",data={"request":"Archive"},files={"files":("policy.docx",raw)},follow_redirects=False)
            self.assertEqual(response.status_code,303)
            url=response.headers["location"]+"/uploads/policy.docx"
            view=client.get(url).text
            self.assertIn("Paragraph 1",view)
            self.assertIn("Table 1, row 1, column 1",view)
            self.assertEqual(client.get(url+"?download=1").content,raw)

    def test_unreadable_upload_leaves_no_assignment(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with TestClient(create_app(root, object())) as client:
            for name in ("invalid.xlsx", "old.doc"):
                r = client.post("/tasks", data={"request": "Archive"}, files={"files": (name, b"invalid")})
                self.assertEqual(r.status_code, 400)
                if name.endswith(".doc"):
                    self.assertIn("save a .docx copy", r.text)
        self.assertEqual(Store(root).list_tasks(), [])


if __name__ == "__main__":
    import sys
    root = Path(sys.argv[1])
    for folder, rate in (("original", 8), ("revised", 12)):
        (root / folder).mkdir(exist_ok=True, parents=True)
        (root / folder / "budget.xlsx").write_bytes(budget(rate))
    (root / "policy.docx").write_bytes(word_policy())
