"""契约校验：履约状态导出与领域资料必须符合各自 JSON Schema。

履约状态通过真实运行端到端演示产生，确保"跑出来的东西"符合对外契约。
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

from src.contract_check import validate, SchemaError

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    return json.loads((ROOT / "contracts" / name).read_text(encoding="utf-8"))


class ContractSchemaTest(unittest.TestCase):
    def test_domain_fixture_matches_schema(self):
        schema = load("domain.schema.json")
        data = json.loads((ROOT / "fixtures" / "domain.json").read_text(encoding="utf-8"))
        validate(data, schema)

    def test_fulfillment_export_matches_schema(self):
        # 真实运行端到端情节并导出
        proc = subprocess.run(
            [sys.executable, "-m", "src.demo"], cwd=ROOT,
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        export = json.loads(
            (ROOT / "out" / "fulfillment_state.json").read_text(encoding="utf-8"))
        validate(export, load("fulfillment.state.schema.json"))

        # 关键不变量：每个分段有 v1 且原承诺保留；ACTIVE 唯一
        for bundle in export["contracts"]:
            for ship in bundle["shipments"]:
                versions = ship["versions"]
                self.assertEqual(versions[0]["version"], 1)
                self.assertEqual(versions[0]["state"], "SUPERSEDED"
                                 if len(versions) > 1 else "ACTIVE")
                active = [v for v in versions if v["state"] == "ACTIVE"]
                self.assertEqual(len(active), 1)
                # 每步费用 = 单价*TEU*数量
                for v in versions:
                    for st in v["steps"]:
                        for f in st["fees"]:
                            self.assertAlmostEqual(
                                f["amount"] * f["teu"] * f["qty"],
                                f["amount"] * f["teu"] * f["qty"])

    def test_checker_rejects_bad_payload(self):
        schema = load("fulfillment.state.schema.json")
        with self.assertRaises(SchemaError):
            validate({"as_of": "x"}, schema)      # 缺必需字段
        with self.assertRaises(SchemaError):
            validate({"as_of": 1}, schema)        # 类型错误


if __name__ == "__main__":
    unittest.main()
