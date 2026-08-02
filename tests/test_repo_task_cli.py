from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReverseRepoTaskCliTests(unittest.TestCase):
    def test_rr_cert_dispatches_all_supported_operations(self):
        command = (ROOT / "rr.cmd").read_text(encoding="utf-8")
        self.assertIn('if /I "%~1"=="cert"', command)
        self.assertIn("-Action Cert -CertDate", command)
        self.assertIn("-Action CertStatus", command)
        self.assertIn("-Action CertDisable", command)
        self.assertIn("-Action CertRemove", command)

    def test_manager_help_puts_live_commands_before_simulation_tools(self):
        manager = (
            ROOT / "scripts" / "manage_reverse_repo_tasks.ps1"
        ).read_text(encoding="utf-8")
        live = manager.index("【实盘任务：关键命令】")
        certification = manager.index("【模拟能力认证：实盘前必须完成】")
        stress = manager.index("【一次性模拟压力测试：不替代能力认证】")
        mail = manager.index("【邮件与帮助】")
        self.assertLess(live, certification)
        self.assertLess(certification, stress)
        self.assertLess(stress, mail)
        self.assertIn('"CertStatus"', manager)
        self.assertIn("Get-SimulationCertificationTaskStatus", manager)

    def test_certification_installer_has_no_stale_fixed_default_date(self):
        installer = (
            ROOT
            / "scripts"
            / "install_repo_simulation_validation_tasks.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("[datetime]::MinValue", installer)
        self.assertNotIn('2026-08-03', installer)
        self.assertIn("Simulation certification date must be a weekday", installer)

    def test_readme_exposes_short_live_and_cert_commands_first(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        quick = readme.index("## 命令速查：实盘优先")
        live = readme.index("### 实盘任务：关键命令", quick)
        certification = readme.index(
            "### 模拟能力认证：实盘前必须完成",
            quick,
        )
        maintenance = readme.index("## 附录F：底层维护入口")
        long_command = readme.index(
            "install_repo_simulation_validation_tasks.ps1"
        )
        self.assertLess(live, certification)
        self.assertGreater(long_command, maintenance)
        self.assertIn(".\\rr cert stat", readme)

    def test_readme_ends_with_pdf_instructions_then_disclaimer(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        repository = readme.index("https://gitee.com/smhe/reverse_repo")
        strategy = readme.index("## 核心策略")
        pdf_instructions = readme.index("## 附录G：README与PDF生成")
        disclaimer = readme.index("## 免责声明")
        self.assertLess(repository, strategy)
        self.assertLess(pdf_instructions, disclaimer)
        self.assertEqual(readme.count(".\\build_readme_pdf.ps1"), 1)
        self.assertTrue(readme.rstrip().endswith("验证和启用流程。"))


if __name__ == "__main__":
    unittest.main()
