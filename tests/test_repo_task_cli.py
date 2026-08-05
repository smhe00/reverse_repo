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

    def test_rr_up_dispatches_the_protected_no_git_updater(self):
        command = (ROOT / "rr.cmd").read_text(encoding="utf-8")
        updater = (
            ROOT / "scripts" / "update_reverse_repo.ps1"
        ).read_text(encoding="utf-8")
        installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
        verifier = (ROOT / "verify.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('if /I "%~1"=="up"', command)
        self.assertIn('set "UPDATER=%~dp0scripts\\update_reverse_repo.ps1"', command)
        self.assertIn('"%UPDATER%" -Destination "%~dp0."', command)
        self.assertIn("Release package checksum mismatch", updater)
        self.assertIn("Release package targets protected local state", updater)
        self.assertIn("rr up is for no-Git installations", updater)
        self.assertIn("restoring previous program files", updater)
        self.assertIn("repo_live_enable_manifest.local.json", updater)
        self.assertIn("release_files.local.json", updater)
        self.assertIn("A reverse-repo task started during update", updater)
        self.assertIn("release_files.local.json", installer)
        self.assertIn("$releaseFiles", installer)
        self.assertIn("test_anonymous_update.ps1", verifier)

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
        self.assertIn('Join-Path $installRoot "userdata_mini"', initializer)
        self.assertIn("勾选【独立交易】并登录一次", initializer)
        self.assertIn("输入Y重试，输入N退出", initializer)
        self.assertIn("两个路径可能填反了", initializer)
        self.assertIn("Get-RunningMiniQmtInstallRoot", initializer)
        self.assertIn("Name='XtMiniQmt.exe'", initializer)
        self.assertIn(".\\rr ui", initializer)
        self.assertIn("-DetectedInstallRoot $detectedLiveRoot", initializer)
        self.assertNotIn(
            "-DetectedInstallRoot $detectedSimulationRoot",
            initializer,
        )
        self.assertNotIn('"D:\\国金QMT交易端模拟"', initializer)
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
        verifier = (ROOT / "verify.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("function Read-ReverseRepoJson", runtime)
        self.assertIn("System.Text.UTF8Encoding($false, $true)", runtime)
        self.assertIn(
            "Read-ReverseRepoJson -Path $runtimeConfigPath",
            initializer,
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
        self.assertIn("-Action LiveCert", command)
        self.assertIn("-Action LiveCertStatus", command)
        self.assertIn("-Action LiveCertReset", command)
        self.assertIn('if /I "%~1"=="dev"', command)
        self.assertIn("-Action DevBind", command)
        self.assertIn("-Action DevCert -CertDate", command)
        self.assertIn("-Action DevStress -StressDate", command)
        self.assertIn("-Action DevStatus", command)
        self.assertNotIn("-Action Cert -CertDate", command)
        self.assertNotIn('if /I "%~1"=="stress"', command)
        self.assertNotIn("-Action ResetCertificate", command)
        manager = (
            ROOT / "scripts" / "manage_reverse_repo_tasks.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn('"LiveCertPreflight"', manager)
        self.assertIn("Invoke-LiveChannelCertificationPreflight", manager)
        self.assertIn('"DevCertStatus"', manager)
        self.assertIn('"DevStressStatus"', manager)
        self.assertIn("Connect-DeveloperSimulationBinding", manager)
        self.assertNotIn('"CertStatus"', manager)
        self.assertNotIn('"StressStatus"', manager)
        self.assertNotIn('"ResetCertificate"', manager)

    def test_live_certification_archives_stale_evidence_on_success(self):
        wrapper = (
            ROOT / "scripts" / "run_repo_live_channel_validation.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("Archive stale certification evidence", wrapper)
        self.assertIn("$keepPaths", wrapper)
        self.assertIn("$staleItems", wrapper)
        self.assertIn("revoked", wrapper)
        validator = (
            ROOT / "scripts" / "repo_live_channel_validation.py"
        ).read_text(encoding="utf-8")
        self.assertIn("notify-success", validator)
        self.assertIn("notify_journal_certification", validator)
        self.assertIn("operational guard", wrapper)
        self.assertIn("A live-channel certificate already exists", wrapper)

    def test_rr_cfg_is_transactional_and_requires_live_tasks_off(self):
        command = (ROOT / "rr.cmd").read_text(encoding="utf-8")
        configurator_path = (
            ROOT / "scripts" / "configure_reverse_repo_strategy.ps1"
        )
        configurator = configurator_path.read_text(encoding="ascii")
        execution_spec = (
            ROOT / "scripts" / "repo_execution_state_machine.py"
        ).read_text(encoding="utf-8")
        self.assertIn('if /I "%~1"=="cfg"', command)
        self.assertIn("configure_reverse_repo_strategy.ps1", command)
        self.assertIn("Assert-LiveStrategyIsOff", configurator)
        self.assertIn('[string]$task.State -ne "Disabled"', configurator)
        self.assertIn("Get-ReverseRepoLiveEnableManifestPath", configurator)
        self.assertIn("Write-BytesAtomically", configurator)
        self.assertIn("$originalBytes", configurator)
        self.assertIn("Running full local verification", configurator)
        self.assertIn("-File $verifyPath", configurator)
        self.assertIn("Verified candidate parameters", configurator)
        self.assertIn("Previous runtime configuration restored", configurator)
        self.assertIn('"config\\runtime.example.json"', configurator)
        self.assertIn("0..1 inclusive", configurator)
        self.assertIn("at least first+5m", configurator)
        self.assertIn("[1/4] First execution time", configurator)
        self.assertIn("[4/4] Second cash usage ratio", configurator)
        self.assertIn("Enter=keep current | D=use default | Q=cancel", configurator)
        self.assertIn("Save the verified parameters?", configurator)
        self.assertNotIn(
            '"configure_reverse_repo_strategy.ps1"',
            execution_spec,
        )

    def test_rr_ui_is_a_loopback_only_whitelisted_console(self):
        command = (ROOT / "rr.cmd").read_text(encoding="utf-8")
        wrapper = (
            ROOT / "scripts" / "run_reverse_repo_web_ui.ps1"
        ).read_text(encoding="utf-8-sig")
        server = (
            ROOT / "scripts" / "reverse_repo_web_ui.py"
        ).read_text(encoding="utf-8")
        frontend = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        configurator = (
            ROOT / "scripts" / "configure_reverse_repo_strategy.ps1"
        ).read_text(encoding="ascii")
        self.assertIn('if /I "%~1"=="ui"', command)
        self.assertIn("run_reverse_repo_web_ui.ps1", command)
        self.assertIn("reverse_repo_web_ui.py", wrapper)
        self.assertIn('LOOPBACK_HOST = "127.0.0.1"', server)
        self.assertNotIn('"0.0.0.0"', server)
        self.assertIn("ACTION_SPECS", server)
        self.assertNotIn('"verify": ActionSpec', server)
        self.assertIn('"live_cert_reset": ActionSpec', server)
        self.assertIn('"wx_test": ActionSpec', server)
        self.assertIn("wx_test", frontend)
        self.assertIn("X-RR-Token", server)
        self.assertIn("Invalid request origin", server)
        self.assertIn("shell=False", server)
        self.assertIn("-NonInteractiveConfirmed", server)
        self.assertIn("prepare_shutdown", server)
        self.assertIn('route == "/api/shutdown"', server)
        self.assertIn("A background operation is still running", server)
        self.assertIn('window.location.replace("about:blank")', frontend)
        self.assertIn("renderTaskStatus", frontend)
        self.assertIn("REVOKE LIVE CERT", frontend)
        self.assertIn("撤销实盘认证", frontend)
        self.assertIn("已存在实盘认证证书", frontend)
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("cli-hint", html)
        self.assertIn("rr on", html)
        self.assertIn("rr cert stat", html)
        self.assertIn("rr cert reset", html)
        self.assertIn("[switch]$NonInteractiveConfirmed", configurator)
        for name in ("index.html", "app.js", "style.css"):
            self.assertTrue((ROOT / "web" / name).is_file())

    def test_rr_notification_commands_dispatch_wxpusher_and_mail(self):
        command = (ROOT / "rr.cmd").read_text(encoding="utf-8")
        manager = (
            ROOT / "scripts" / "manage_reverse_repo_tasks.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn('if /I "%~1"=="mail"', command)
        self.assertIn('if /I "%~1"=="mt"', command)
        self.assertIn('if /I "%~1"=="wx"', command)
        self.assertIn('if /I "%~1"=="wt"', command)
        self.assertIn("-Action ConfigureWxPusher", command)
        self.assertIn("-Action TestWxPusher", command)
        self.assertIn('"ConfigureWxPusher"', manager)
        self.assertIn('"TestWxPusher"', manager)
        self.assertIn("Configure-WxPusher", manager)
        self.assertIn("Test-WxPusher", manager)
        self.assertIn("configure_repo_failure_wxpusher.ps1", manager)
        self.assertIn("MINIQMT_ALERT_WXPUSHER_TOKEN", manager)

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
            "miniQMT SIM Repo V3 Certificate",
        ):
            self.assertIn(task_name, manager)
        self.assertIn("残留0项", manager)
        self.assertIn("运行中的任务不会被强制终止", manager)
        self.assertNotIn("Stop-ScheduledTask", manager)
        self.assertIn(".\\rr clear", readme)

    def test_manager_help_keeps_only_live_certification_commands(self):
        manager = (
            ROOT / "scripts" / "manage_reverse_repo_tasks.ps1"
        ).read_text(encoding="utf-8")
        live = manager.index("【实盘任务：关键命令】")
        certification = manager.index("【快速实盘通道认证：固定1000元】")
        mail = manager.index("【通知与帮助】")
        self.assertLess(live, certification)
        self.assertLess(certification, mail)
        self.assertNotIn("【完整模拟能力认证", manager)
        self.assertNotIn("【一次性模拟压力测试", manager)
        self.assertIn("【开发者模拟验证与压力测试", manager)
        self.assertIn("Get-SimulationCertificationTaskStatus", manager)
        self.assertIn("Get-SimulationStressTaskStatus", manager)
        self.assertIn("Reset-SimulationCertificate", manager)
        self.assertIn("Install-SimulationCertificationTasks", manager)
        self.assertIn("Install-SimulationStressTask", manager)

    def test_rr_on_reconciles_tasks_before_arming_live_execution(self):
        manager = (
            ROOT / "scripts" / "manage_reverse_repo_tasks.ps1"
        ).read_text(encoding="utf-8")
        enabled = manager.split(
            "function Set-ManagedTasksEnabled",
            1,
        )[1].split("function Assert-ManagedTasksMatchConfig", 1)[0]
        install = enabled.index("Install-ManagedTasks")
        schedule_check = enabled.index("Assert-ManagedTasksMatchConfig")
        gate = enabled.index("Assert-LiveEnableGate")
        manifest = enabled.index("New-ReverseRepoLiveEnableManifest")
        self.assertLess(install, schedule_check)
        self.assertLess(schedule_check, gate)
        self.assertLess(gate, manifest)
        self.assertIn("Cannot install or update running live tasks", manager)

    def test_certification_installer_has_no_stale_fixed_default_date(self):
        installer = (
            ROOT
            / "scripts"
            / "install_repo_simulation_validation_tasks.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("[datetime]::MinValue", installer)
        self.assertNotIn('2026-08-03', installer)
        self.assertIn("Simulation certification date must be a weekday", installer)
        self.assertIn("Find-IsolatedRecoveryExecutionTime", installer)
        self.assertIn("miniQMT SIM Repo V3 Morning Normal", installer)
        self.assertIn("miniQMT SIM Repo V3 Afternoon Normal", installer)
        self.assertIn("miniQMT SIM Repo V3 Morning Recovery", installer)
        self.assertIn("miniQMT SIM Repo V3 Certificate", installer)
        self.assertIn("-LeadSeconds 162", installer)

    def test_normal_certification_uses_production_entry_and_isolates_faults(self):
        normal = (
            ROOT
            / "scripts"
            / "run_repo_simulation_morning_normal_validation.ps1"
        ).read_text(encoding="utf-8")
        recovery = (
            ROOT
            / "scripts"
            / "run_repo_simulation_morning_recovery_validation.ps1"
        ).read_text(encoding="utf-8-sig")
        certificate = (
            ROOT / "scripts" / "run_repo_simulation_certificate.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("gc001_live_daily_90pct_093042.py", normal)
        self.assertIn('"--environment"', normal)
        self.assertIn('"simulation"', normal)
        self.assertIn('"--maximum-principal-yuan"', normal)
        self.assertIn('"repo_morn_norm"', normal)
        self.assertNotIn("prepare_repo_simulation_morning_recovery.py", normal)
        self.assertIn("prepare_repo_simulation_morning_recovery.py", recovery)
        self.assertIn('"repo_morn_rec"', recovery)
        self.assertIn("simulation_normal_execution.lock", normal)
        self.assertIn("simulation_fault_execution.lock", recovery)
        self.assertIn("--morning-normal-journal", certificate)
        self.assertIn("--afternoon-normal-journal", certificate)
        self.assertIn("--morning-recovery-journal", certificate)

    def test_readme_places_guided_quick_start_before_command_reference(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        core = readme.index("## 核心策略")
        quick = readme.index("## 快速开始（Getting Started）")
        commands = readme.index("## 命令参考（熟悉流程后使用）")
        configuration = readme.index("## 策略配置")
        self.assertLess(core, quick)
        self.assertLess(quick, commands)
        self.assertLess(commands, configuration)
        for number in range(1, 8):
            self.assertIn(f'<a id="quick-step-{number}"></a>', readme)
        self.assertIn("#details-config", readme)
        self.assertIn("#details-validation", readme)
        self.assertIn("#details-operations", readme)
        self.assertIn("#details-strategy", readme)
        self.assertIn("#details-init", readme)
        self.assertIn(".\\rr cert stat", readme)
        self.assertNotIn(
            "install_repo_simulation_validation_tasks.ps1",
            readme,
        )

    def test_quick_start_is_ui_first_and_keeps_safety_review(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        quick = readme[
            readme.index("## 快速开始（Getting Started）"):
            readme.index("## 命令参考（熟悉流程后使用）")
        ]
        manual = quick.index("第3步：在网页控制台检查并保存四项参数（必做）")
        validation = quick.index("第4步：在页面复核状态")
        certification = quick.index("第5步：完成1000元实盘快速认证")
        self.assertLess(manual, validation)
        self.assertLess(validation, certification)
        self.assertIn(".\\rr ui", quick)
        self.assertIn("怎么打开网页控制台", quick)
        self.assertIn("验证并保存参数", quick)
        self.assertIn("LIVE 1000", quick)
        self.assertIn("自动运行", quick)
        self.assertIn("自动恢复原配置", quick)
        self.assertIn("ENABLE LIVE", quick)
        for field in (
            "first_execution_time",
            "first_cash_usage_ratio",
            "second_execution_time",
            "second_cash_usage_ratio",
        ):
            self.assertIn(field, quick)
        self.assertIn("完整`verify.ps1`", quick)
        self.assertIn("此时必须停下来，由人逐项核对", quick)
        self.assertNotIn(".\\rr cfg", quick)

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
        self.assertIn("不影响只读或历史任务", commands)
        self.assertNotIn("模拟认证", commands)
        self.assertNotIn("压力测试", commands)
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
        self.assertIn("[switch]$SkipUi", installer)
        self.assertIn('"rr.cmd") ui', installer)
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
        pdf_instructions = readme.index("## 附录G：README与PDF生成")
        disclaimer = readme.index("## 免责声明")
        self.assertLess(repository, strategy)
        self.assertLess(pdf_instructions, disclaimer)
        self.assertEqual(readme.count(".\\build_readme_pdf.ps1"), 1)
        self.assertTrue(readme.rstrip().endswith("验证和启用流程。"))


if __name__ == "__main__":
    unittest.main()
