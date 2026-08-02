from __future__ import annotations

import unittest
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
            "9dc4d0b051bfd5b881f10846ee023fd7",
            initializer,
        )
        self.assertIn("mirrors.huaweicloud.com/python/3.12.10", initializer)
        self.assertIn("registry.npmmirror.com/-/binary/python/3.12.10", initializer)
        self.assertIn("python-3.12.10-amd64.zip", initializer)
        self.assertIn("$pythonPackageSize = 32399384", initializer)
        self.assertIn("foreach ($source in $pythonPackageSources)", initializer)
        self.assertIn("所有便携Python下载源均失败", initializer)
        self.assertIn("pypi.tuna.tsinghua.edu.cn", initializer)
        self.assertIn("Assert-LiveTasksInactive", initializer)
        self.assertIn("Install-PortablePython", initializer)
        self.assertIn("ExtractToDirectory", initializer)
        self.assertIn("Unsafe path in portable Python package", initializer)
        self.assertIn('"Lib/venv/__init__.py"', initializer)
        self.assertIn('"Lib/ensurepip/__init__.py"', initializer)
        self.assertIn('"venvlauncher.exe"', initializer)
        self.assertIn('"venvwlauncher.exe"', initializer)
        self.assertNotIn("Start-Process", initializer)
        self.assertNotIn("Find-CompatibleBasePython", initializer)
        self.assertNotIn("HKCU:", initializer)
        self.assertNotIn("HKLM:", initializer)
        self.assertNotIn("https://www.python.org/ftp", initializer)
        self.assertNotIn("gitee.com/smhe/reverse_repo/raw/main/dist", initializer)

    def test_repeated_initialization_skips_healthy_python_dependencies(self):
        initializer = (
            ROOT / "scripts" / "initialize_reverse_repo.ps1"
        ).read_text(encoding="utf-8-sig")
        runtime = (
            ROOT / "scripts" / "reverse_repo_runtime.ps1"
        ).read_text(encoding="utf-8-sig")
        ready = initializer.index("if (Test-VirtualEnvironmentReady)")
        mirrors = initializer.index(
            "$orderedMirrors = Get-ReachableMirrors",
            ready,
        )
        self.assertLess(ready, mirrors)
        self.assertIn("reverse_repo_dependencies.json", initializer)
        self.assertIn("requirements_sha256", initializer)
        self.assertIn("-m pip check", initializer)
        self.assertIn("跳过联网安装", initializer)
        self.assertIn("Save-VirtualEnvironmentState", initializer)
        self.assertIn("function Get-ReverseRepoSha256", runtime)
        self.assertNotIn("Get-FileHash", initializer)

    def test_local_verification_reuses_existing_portable_runtime(self):
        verifier = (ROOT / "verify.ps1").read_text(encoding="utf-8-sig")
        portable_test = (
            ROOT / "tests" / "test_portable_python_runtime.ps1"
        ).read_text(encoding="utf-8-sig")
        self.assertIn('"-UseExistingRuntime"', verifier)
        self.assertIn("param([switch]$UseExistingRuntime)", portable_test)
        self.assertIn("-not $UseExistingRuntime", portable_test)

    def test_python_runtime_is_not_bundled_in_current_tree(self):
        bundled = list((ROOT / "dist").glob("python-3.12.10-portable*"))
        self.assertEqual(bundled, [])

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
        self.assertIn("Get-RunningMiniQmtInstallRoot", initializer)
        self.assertIn("Name='XtMiniQmt.exe'", initializer)
        self.assertIn("-DetectedInstallRoot $detectedLiveRoot", initializer)
        self.assertIn(
            "-DetectedInstallRoot $detectedSimulationRoot",
            initializer,
        )
        self.assertNotIn('Prompt "实盘miniQMT路径"', initializer)

    def test_account_binding_returns_only_a_boolean_success_value(self):
        initializer = (
            ROOT / "scripts" / "initialize_reverse_repo.ps1"
        ).read_text(encoding="utf-8")
        body = initializer.split("function Initialize-AccountBinding", 1)[1]
        body = body.split("Assert-LiveTasksInactive", 1)[0]
        self.assertNotIn("Write-Output", body)
        self.assertIn("Write-Host", body)
        self.assertIn("Out-Host", body)

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

    def test_all_powershell_json_inputs_use_explicit_utf8_reader(self):
        runtime = (
            ROOT / "scripts" / "reverse_repo_runtime.ps1"
        ).read_text(encoding="utf-8-sig")
        initializer = (
            ROOT / "scripts" / "initialize_reverse_repo.ps1"
        ).read_text(encoding="utf-8-sig")
        stress = (
            ROOT / "scripts" / "install_repo_simulation_stress_task.ps1"
        ).read_text(encoding="utf-8-sig")
        verifier = (ROOT / "verify.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("function Read-ReverseRepoJson", runtime)
        self.assertIn("System.Text.UTF8Encoding($false, $true)", runtime)
        self.assertIn(
            "Read-ReverseRepoJson -Path $runtimeConfigPath",
            initializer,
        )
        self.assertIn(
            "Read-ReverseRepoJson -Path $simulationBindingPath",
            stress,
        )
        self.assertIn("test_windows_powershell_utf8_json.ps1", verifier)

    def test_initializer_verification_ignores_maintainer_release_artifacts(self):
        initializer = (
            ROOT / "scripts" / "initialize_reverse_repo.ps1"
        ).read_text(encoding="utf-8-sig")
        verifier = (ROOT / "verify.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("-Initialization", initializer)
        self.assertIn("param([switch]$Initialization)", verifier)
        self.assertIn("tmp\\initialization_verification", verifier)
        self.assertIn("-not $Initialization", verifier)

    def test_rr_cert_dispatches_all_supported_operations(self):
        command = (ROOT / "rr.cmd").read_text(encoding="utf-8")
        self.assertIn('if /I "%~1"=="cert"', command)
        self.assertIn("-Action Cert -CertDate", command)
        self.assertIn("-Action CertStatus", command)
        self.assertIn("-Action CertDisable", command)
        self.assertIn("-Action CertRemove", command)

    def test_rr_clear_removes_only_known_project_tasks(self):
        command = (ROOT / "rr.cmd").read_text(encoding="utf-8")
        manager = (
            ROOT / "scripts" / "manage_reverse_repo_tasks.ps1"
        ).read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn('if /I "%~1"=="clear"', command)
        self.assertIn("-Action Clear", command)
        self.assertIn('"Clear"', manager)
        self.assertIn("Clear-AllReverseRepoTasks", manager)
        self.assertIn("$allReverseRepoTaskNames", manager)
        for task_name in (
            "miniQMT Reverse Repo First",
            "miniQMT Reverse Repo Second",
            "miniQMT LIVE READONLY Morning",
            "miniQMT LIVE READONLY Afternoon",
            "miniQMT SIM Interface Stress 5Hz",
            "miniQMT SIM Repo V2 Certificate",
        ):
            self.assertIn(task_name, manager)
        self.assertIn("残留0项", manager)
        self.assertIn("运行中的任务不会被强制终止", manager)
        self.assertNotIn("Stop-ScheduledTask", manager)
        self.assertIn(".\\rr clear", readme)

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
        for number in range(1, 8):
            self.assertIn(f'<a id="quick-step-{number}"></a>', readme)
        self.assertIn("#details-config", readme)
        self.assertIn("#details-validation", readme)
        self.assertIn("#details-operations", readme)
        self.assertIn("#details-strategy", readme)
        self.assertIn("#details-init", readme)
        self.assertIn(".\\rr cert stat", readme)

    def test_quick_start_requires_manual_config_review_before_validation(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        quick = readme[
            readme.index("## 快速开始（Getting Started）"):
            readme.index("## 命令参考（熟悉流程后使用）")
        ]
        manual = quick.index("第3步：人工检查并修改策略参数（必做）")
        validation = quick.index("第4步：应用配置并完成本地验证")
        certification = quick.index("第5步：用模拟账户完成一个交易日认证")
        self.assertLess(manual, validation)
        self.assertLess(validation, certification)
        self.assertIn("notepad .\\config\\runtime.local.json", quick)
        self.assertIn("必须由人打开配置文件", quick)
        for field in (
            "first_execution_time",
            "first_cash_usage_ratio",
            "second_execution_time",
            "second_cash_usage_ratio",
        ):
            self.assertIn(field, quick)
        self.assertIn("verify.ps1`失败时应先修正配置，不能继续", quick)
        self.assertIn("此时必须停下来，由人逐项核对", quick)

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
        self.assertIn(
            "irm https://gitee.com/smhe/reverse_repo/raw/main/install.ps1 | iex",
            quick,
        )
        self.assertIn("无需安装或学习Git", quick)
        self.assertNotIn("下载或克隆本仓库", quick)

    def test_readme_command_table_distinguishes_task_removal_scope(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        commands = readme[
            readme.index("## 命令参考（熟悉流程后使用）"):
            readme.index("## 策略配置")
        ]
        self.assertIn("| 命令 | 用途 | 与相近命令的差别 |", commands)
        self.assertIn('`off`是“暂停并保留”', commands)
        self.assertIn('`del`是“只删实盘”', commands)
        self.assertIn('`clear`是“清空', commands)
        self.assertIn("不影响模拟认证、压力或只读任务", commands)
        self.assertIn("本项目已知的全部计划任务", commands)

    def test_readme_hides_internal_qmt_connection_directory(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("userdata_mini", readme)
        self.assertIn("勾选“独立交易”并至少成功登录一次", readme)

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
