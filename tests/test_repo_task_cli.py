from __future__ import annotations

import hashlib
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReverseRepoTaskCliTests(unittest.TestCase):
    def test_rr_init_uses_inbox_windows_powershell(self):
        command = (ROOT / "rr.cmd").read_text(encoding="utf-8")
        self.assertIn('if /I "%~1"=="init"', command)
        self.assertIn("WindowsPowerShell\\v1.0\\powershell.exe", command)
        self.assertIn("-Action Initialize", command)
        self.assertNotIn("pwsh", command.lower())

    def test_initializer_bootstraps_verified_portable_python(self):
        initializer = (
            ROOT / "scripts" / "initialize_reverse_repo.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn('$pythonVersion = "3.12.10"', initializer)
        self.assertIn(
            "bbda4dcf688a94211b62d50968a91b38",
            initializer,
        )
        self.assertIn("pypi.tuna.tsinghua.edu.cn", initializer)
        self.assertIn("Assert-LiveTasksInactive", initializer)
        self.assertIn("Install-PortablePython", initializer)
        self.assertIn("python-3.12.10-portable.nupkg", initializer)
        self.assertIn("ExtractToDirectory", initializer)
        self.assertIn("Unsafe path in portable Python package", initializer)
        self.assertNotIn("Start-Process", initializer)
        self.assertNotIn("Find-CompatibleBasePython", initializer)
        self.assertNotIn("HKCU:", initializer)
        self.assertNotIn("HKLM:", initializer)
        self.assertNotIn("https://www.python.org/ftp", initializer)

    def test_bundled_python_is_pinned_complete_nuget_runtime(self):
        package = ROOT / "dist" / "python-3.12.10-portable.nupkg"
        digest = hashlib.sha512(package.read_bytes()).hexdigest()
        self.assertEqual(
            digest,
            "bbda4dcf688a94211b62d50968a91b38"
            "f305d0b8d1ecd90269f74a86f8a0a4fc"
            "ebb7ca162a0753a47691eb3df0c964009"
            "bd3d8194c6fd19afae8d5fd01e1cc0f",
        )
        with zipfile.ZipFile(package) as archive:
            names = set(archive.namelist())
        self.assertIn("tools/python.exe", names)
        self.assertIn("tools/Lib/venv/__init__.py", names)
        self.assertIn("tools/Lib/ensurepip/__init__.py", names)

    def test_initializer_accepts_qmt_install_roots_and_waits_for_userdata(self):
        initializer = (
            ROOT / "scripts" / "initialize_reverse_repo.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn('"D:\\国金证券QMT交易端"', initializer)
        self.assertIn('"D:\\国金QMT交易端模拟"', initializer)
        self.assertIn('Join-Path $installRoot "userdata_mini"', initializer)
        self.assertIn("勾选【独立交易】并登录一次", initializer)
        self.assertIn("输入Y重试，输入N退出", initializer)
        self.assertIn("两个路径可能填反了", initializer)
        self.assertNotIn('Prompt "实盘miniQMT路径"', initializer)

    def test_runtime_is_pinned_and_powershell_51_compatible(self):
        requirements = (ROOT / "requirements.txt").read_text(
            encoding="utf-8"
        )
        runtime = (
            ROOT / "scripts" / "reverse_repo_runtime.ps1"
        ).read_text(encoding="utf-8")
        manager = (
            ROOT / "scripts" / "manage_reverse_repo_tasks.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("xtquant==250516.1.1", requirements)
        self.assertNotIn("::IsFinite", runtime)
        self.assertIn("::IsNaN", runtime)
        self.assertIn("::IsInfinity", runtime)
        self.assertNotIn("PowerShell 7", manager)
        self.assertNotIn("pwsh.exe", manager)
        self.assertIn("action.Execute -ieq $expectedPowerShell", manager)
        self.assertIn("action.Execute -ine $expectedPowerShell", manager)

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

    def test_readme_places_guided_quick_start_before_command_reference(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        core = readme.index("## 核心策略")
        quick = readme.index("## 快速开始（Getting Started）")
        commands = readme.index("## 命令参考（熟悉流程后使用）")
        configuration = readme.index("## 策略配置")
        maintenance = readme.index("## 附录F：底层维护入口")
        long_command = readme.index(
            "install_repo_simulation_validation_tasks.ps1"
        )
        self.assertLess(core, quick)
        self.assertLess(quick, commands)
        self.assertLess(commands, configuration)
        self.assertGreater(long_command, maintenance)
        for number in range(1, 7):
            self.assertIn(f'<a id="quick-step-{number}"></a>', readme)
        self.assertIn("#details-config", readme)
        self.assertIn("#details-validation", readme)
        self.assertIn("#details-operations", readme)
        self.assertIn("#details-strategy", readme)
        self.assertIn("#details-init", readme)
        self.assertIn(".\\rr cert stat", readme)

    def test_readme_offers_no_git_single_command_install(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        quick = readme[
            readme.index("## 快速开始（Getting Started）"):
            readme.index("## 命令参考（熟悉流程后使用）")
        ]
        self.assertIn(
            "https://gitee.com/smhe/reverse_repo/raw/main/install.ps1",
            quick,
        )
        self.assertIn("-Destination $PWD.Path", quick)
        self.assertIn("无需安装或学习Git", quick)
        self.assertNotIn("下载或克隆本仓库", quick)

    def test_no_git_installer_has_integrity_and_overwrite_guards(self):
        installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
        builder = (
            ROOT / "scripts" / "build_release_bundle.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("Destination must be an empty directory", installer)
        self.assertIn("Get-FileHash", installer)
        self.assertIn("Assert-SafeArchive", installer)
        self.assertIn("reverse_repo-latest.zip.sha256", installer)
        self.assertIn('"rr.cmd") init', installer)
        self.assertIn("git.exe", builder)
        self.assertIn("ls-files", builder)
        self.assertIn("[switch]$Check", builder)
        verifier = (ROOT / "verify.ps1").read_text(encoding="utf-8")
        self.assertIn('"build_release_bundle.ps1"', verifier)
        self.assertIn("-Check", verifier)

    def test_readme_ends_with_pdf_instructions_then_disclaimer(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        repository = readme.index("https://gitee.com/smhe/reverse_repo")
        strategy = readme.index("## 核心策略")
        pdf_instructions = readme.index("## 附录H：README与PDF生成")
        disclaimer = readme.index("## 免责声明")
        self.assertLess(repository, strategy)
        self.assertLess(pdf_instructions, disclaimer)
        self.assertEqual(readme.count(".\\build_readme_pdf.ps1"), 1)
        self.assertTrue(readme.rstrip().endswith("验证和启用流程。"))


if __name__ == "__main__":
    unittest.main()
