use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::Mutex;

use tauri::Emitter;
use tauri::Manager;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutEvent, ShortcutState};
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_window_state::AppHandleExt;
use tauri_plugin_window_state::StateFlags;

// 窗口状态记忆的落盘维度（插件注册与 CloseRequested/防抖手动保存共用同一组；
// 不含 VISIBLE——主窗口常"隐藏到托盘"后退出，保存可见性会让下次启动不显示）。
// const 上下文组合 bitflags 2.x 须用 .union（| 不是 const fn）
const WINDOW_STATE_FLAGS: StateFlags = StateFlags::SIZE
    .union(StateFlags::POSITION)
    .union(StateFlags::MAXIMIZED);

// 边车子进程句柄；应用退出时取出并 kill，防止僵尸进程
struct SidecarChild(Mutex<Option<CommandChild>>);

// 关闭主窗口的行为："hide" = 隐藏到托盘（默认，与后端 AppSettings.close_action
// 默认一致）/ "quit" = 完全退出。前端拉到设置后经 set_close_behavior 命令同步，
// CloseRequested 拦截读它——启动初期未同步前按默认 hide 兜底
struct CloseBehavior(Mutex<String>);

#[tauri::command]
fn set_close_behavior(behavior: String, state: tauri::State<CloseBehavior>) -> Result<(), String> {
    if behavior != "hide" && behavior != "quit" {
        return Err(format!("非法关闭行为: {behavior}"));
    }
    *state.0.lock().unwrap() = behavior;
    Ok(())
}

// —— 服务端口配置（端口真源 = Tauri 侧配置文件，不进 SQLite：后端绑定端口先于
// 读库——先有端口才能起服务读设置，鸡生蛋死结；且架构红线不让 Rust 读写
// SQLite。设置页"服务端口"行经这两个命令读写，spawn 边车时以 --port N 传后端）——

// 配置文件默认端口（文件缺失/损坏/越界时的兜底）
const DEFAULT_SERVER_PORT: u16 = 8086;

// 配置文件路径：app_config_dir()/server_port.json（Windows 下 %APPDATA%\com.hat.clara\）
fn server_port_path(app: &tauri::AppHandle) -> Option<std::path::PathBuf> {
    app.path()
        .app_config_dir()
        .ok()
        .map(|dir| dir.join("server_port.json"))
}

// 读端口：任何异常（文件缺失/JSON 损坏/端口越界）都回默认——配置读取不阻塞启动
fn read_server_port(app: &tauri::AppHandle) -> u16 {
    let Some(path) = server_port_path(app) else {
        return DEFAULT_SERVER_PORT;
    };
    let Ok(text) = std::fs::read_to_string(&path) else {
        return DEFAULT_SERVER_PORT;
    };
    serde_json::from_str::<serde_json::Value>(&text)
        .ok()
        .and_then(|v| v.get("port").and_then(|p| p.as_u64()))
        .and_then(|p| u16::try_from(p).ok())
        .filter(|p| (1024..=65535).contains(p))
        .unwrap_or(DEFAULT_SERVER_PORT)
}

#[tauri::command]
fn get_server_port(app: tauri::AppHandle) -> u16 {
    read_server_port(&app)
}

#[tauri::command]
fn set_server_port(app: tauri::AppHandle, port: u16) -> Result<(), String> {
    if !(1024..=65535).contains(&port) {
        return Err(format!("端口须在 1024–65535 之间: {port}"));
    }
    let Some(path) = server_port_path(&app) else {
        return Err("无法定位应用配置目录".to_string());
    };
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| format!("创建配置目录失败: {e}"))?;
    }
    let text = serde_json::json!({ "port": port }).to_string();
    std::fs::write(&path, text).map_err(|e| format!("写入端口配置失败: {e}"))
}

// —— 左右栏宽配置（真源 = Tauri 侧配置文件 layout.json，理由同端口不进 SQLite：
// 窗口状态本就由 Rust 在 webview 加载前恢复，布局同域；且后端缺席（端口冲突继续
// 启动）时栏宽恢复不应依赖后端。前端 App.tsx 启动读一次、拖拽松手防抖写回）——

#[derive(serde::Serialize, serde::Deserialize)]
struct PanelLayout {
    left_width: u32,
    right_width: u32,
}

// 配置文件路径：app_config_dir()/layout.json（与 server_port.json 同目录）
fn layout_path(app: &tauri::AppHandle) -> Option<std::path::PathBuf> {
    app.path()
        .app_config_dir()
        .ok()
        .map(|dir| dir.join("layout.json"))
}

// 读布局：任何异常（文件缺失/JSON 损坏/字段缺失/数值越界）都返回 None——前端落默认栏宽
fn read_layout(app: &tauri::AppHandle) -> Option<PanelLayout> {
    let path = layout_path(app)?;
    let text = std::fs::read_to_string(path).ok()?;
    let layout: PanelLayout = serde_json::from_str(&text).ok()?;
    ((1..=4096).contains(&layout.left_width) && (1..=4096).contains(&layout.right_width))
        .then_some(layout)
}

#[tauri::command]
fn get_layout(app: tauri::AppHandle) -> Option<PanelLayout> {
    read_layout(&app)
}

#[tauri::command]
fn set_layout(app: tauri::AppHandle, left_width: u32, right_width: u32) -> Result<(), String> {
    // 防御性宽范围：精确范围（左 160–320 / 右 420–720 与动态上限）由前端拖拽 clamp 保证
    if !(1..=4096).contains(&left_width) || !(1..=4096).contains(&right_width) {
        return Err(format!("栏宽须在 1–4096 之间: {left_width}/{right_width}"));
    }
    let Some(path) = layout_path(&app) else {
        return Err("无法定位应用配置目录".to_string());
    };
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| format!("创建配置目录失败: {e}"))?;
    }
    let text =
        serde_json::json!({ "left_width": left_width, "right_width": right_width }).to_string();
    std::fs::write(&path, text).map_err(|e| format!("写入布局配置失败: {e}"))
}

// —— 桌面小部件显隐状态（真源 = Tauri 侧 widget.json，模式同 server_port.json：
// 写入口三个——托盘 toggle / CloseRequested 拦截 / X 按钮 hide_widget 命令——两个
// 在 Rust，localStorage 够不着；读侧全容错默认 false，首启动/坏数据都视为隐藏，
// 与 conf 的 visible:false 一致）——

fn widget_state_path(app: &tauri::AppHandle) -> Option<std::path::PathBuf> {
    app.path()
        .app_config_dir()
        .ok()
        .map(|dir| dir.join("widget.json"))
}

fn read_widget_visible(app: &tauri::AppHandle) -> bool {
    let Some(path) = widget_state_path(app) else {
        return false;
    };
    let Ok(text) = std::fs::read_to_string(&path) else {
        return false;
    };
    serde_json::from_str::<serde_json::Value>(&text)
        .ok()
        .and_then(|v| v.get("visible").and_then(|b| b.as_bool()))
        .unwrap_or(false)
}

fn write_widget_visible(app: &tauri::AppHandle, visible: bool) {
    // 写失败仅日志不中断显隐操作（配置脏一拍由下一任一显隐入口自愈）
    let Some(path) = widget_state_path(app) else {
        eprintln!("无法定位应用配置目录，跳过小部件显隐写入");
        return;
    };
    if let Some(dir) = path.parent() {
        if let Err(e) = std::fs::create_dir_all(dir) {
            eprintln!("创建配置目录失败: {e}");
            return;
        }
    }
    if let Err(e) = std::fs::write(&path, serde_json::json!({ "visible": visible }).to_string())
    {
        eprintln!("写入小部件显隐状态失败: {e}");
    }
}

// 前端 widget 挂载、几何恢复完成后读一次，true 则 JS 自行 show（几何就绪后再
// 显示，防 conf 默认位置闪现跳动）
#[tauri::command]
fn get_widget_visible(app: tauri::AppHandle) -> bool {
    read_widget_visible(&app)
}

// X 按钮关闭：写 false + hide 一步到位（写在前——hide 失败时状态=隐藏恰合用户意图）
#[tauri::command]
fn hide_widget(app: tauri::AppHandle) {
    write_widget_visible(&app, false);
    if let Some(win) = app.get_webview_window("widget") {
        let _ = win.hide();
    }
}

// —— 全局快捷键（2026-11-05 增）：绑定表真源 = Tauri 侧 shortcuts.json（同
// layout.json / widget.json 先例——Rust 启动即需读表注册，后端缺席也要可用，
// 后端 SQLite 零参与）。动作语义：窗口级动作 Rust 直接执行；窗内动作 show_main
// 唤窗后 emit 事件 "app-shortcut" 给主窗口前端二级分发（见 docs/shortcuts.md）。
// 改键不重建 handler——handler 每次按下反查 ShortcutStore 当前绑定表定位动作 ——

// 全部动作与默认键单源（id → 键位串；全串可 parse 由文件尾单测把关，
// `[` `]` `\` 的字面拼写以 parse 结果定案）
// 三页切换曾用 Ctrl+Alt+1/2/3，Windows 真机 RegisterHotKey 报
// ERROR_HOTKEY_ALREADY_REGISTERED（1409，被外部应用 OS 级抢占，非本方重复注册），
// 故改用 F1-F3（实测空闲且与页码顺序对应）。
const SHORTCUT_DEFAULTS: &[(&str, &str)] = &[
    ("toggle-main", "Ctrl+Alt+C"),
    ("toggle-widget", "Alt+\\"),
    ("view-schedule", "Ctrl+Alt+F1"),
    ("view-inbox", "Ctrl+Alt+F2"),
    ("view-datasource", "Ctrl+Alt+F3"),
    ("toggle-right", "Ctrl+Alt+R"),
    ("view-day", "Ctrl+Alt+D"),
    ("view-week", "Ctrl+Alt+W"),
    ("view-month", "Ctrl+Alt+M"),
    ("go-today", "Ctrl+Alt+T"),
    ("open-search", "Ctrl+Alt+F"),
    ("prev-period", "Alt+["),
    ("next-period", "Alt+]"),
    ("quick-add", "Ctrl+Alt+N"),
    ("open-assistant", "Ctrl+Alt+Z"),
    ("sync-now", "Ctrl+Alt+S"),
];

// 绑定表 live 态（id → 键位串）+ 注册失败集合（当前绑定键被外部应用抢占的动作 id，
// 设置页据此标橙「被占用」；改键成功即摘除、恢复默认整体重算——不做运行期自愈：
// 抢占者退出释放热键后需改键或重启才清）。改键命令更新后立即落盘 shortcuts.json
#[derive(Default, Clone, serde::Serialize)]
struct ShortcutTable {
    bindings: HashMap<String, String>,
    failed: std::collections::HashSet<String>,
}
struct ShortcutStore(Mutex<ShortcutTable>);

// 键等值比较：语义等价即 mods + key（不整值 ==——Shortcut.id 是内部编码不参与语义）
fn same_shortcut(a: &Shortcut, b: &Shortcut) -> bool {
    a.mods == b.mods && a.key == b.key
}

fn shortcuts_path(app: &tauri::AppHandle) -> Option<std::path::PathBuf> {
    app.path()
        .app_config_dir()
        .ok()
        .map(|dir| dir.join("shortcuts.json"))
}

// 读绑定表并按默认逐动作合并：缺行/坏行回落默认键（新动作自动带出、旧文件
// 未知 id 自动丢弃），全表恒与 SHORTCUT_DEFAULTS 键集一致
fn read_shortcut_bindings(app: &tauri::AppHandle) -> HashMap<String, String> {
    let saved: HashMap<String, String> = shortcuts_path(app)
        .and_then(|path| std::fs::read_to_string(path).ok())
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default();
    SHORTCUT_DEFAULTS
        .iter()
        .map(|(id, def)| {
            let combo = saved
                .get(*id)
                .cloned()
                .filter(|c| c.parse::<Shortcut>().is_ok())
                .unwrap_or_else(|| def.to_string());
            ((*id).to_string(), combo)
        })
        .collect()
}

fn write_shortcut_bindings(app: &tauri::AppHandle, bindings: &HashMap<String, String>) -> Result<(), String> {
    let Some(path) = shortcuts_path(app) else {
        return Err("无法定位应用配置目录".to_string());
    };
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| format!("创建配置目录失败: {e}"))?;
    }
    let text = serde_json::to_string(bindings).map_err(|e| format!("序列化绑定表失败: {e}"))?;
    std::fs::write(&path, text).map_err(|e| format!("写入快捷键配置失败: {e}"))
}

// 注册/注销单条绑定（调用方已 parse 成 Shortcut，这两函数只管注册动作本身）
fn register_binding(app: &tauri::AppHandle, shortcut: Shortcut) -> Result<(), String> {
    app.global_shortcut()
        .register(shortcut)
        .map_err(|e| format!("注册快捷键失败: {e}"))
}

fn unregister_binding(app: &tauri::AppHandle, shortcut: Shortcut) {
    // 注销错误忽略（该键本就未注册/已被系统回收——都是要重置的状态）
    let _ = app.global_shortcut().unregister(shortcut);
}

// 显示/隐藏主窗口：可见且聚焦 → 隐藏（等价用户点 X 藏到托盘）；否则唤回（复用 show_main）
fn toggle_main(app: &tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let visible = win.is_visible().unwrap_or(false);
        let focused = win.is_focused().unwrap_or(false);
        if visible && focused {
            let _ = win.hide();
        } else {
            show_main(app);
        }
    }
}

// 动作分发：toggle-main / toggle-widget 窗口级 Rust 直接执行；其余窗内动作（含
// sync-now——2026-11-05 起唤窗切数据源页看采集转圈，与手动点击刷新同款反馈，
// 推翻早前「不唤窗走系统通知」口径）先唤主窗再转发前端二级分发
fn dispatch_shortcut(app: &tauri::AppHandle, action: &str) {
    match action {
        "toggle-main" => toggle_main(app),
        "toggle-widget" => toggle_widget(app),
        _ => {
            show_main(app);
            let _ = app.emit_to("main", "app-shortcut", action);
        }
    }
}

// 全局快捷键回调：只响应按下沿（Released 也触发，不判会双执行）；反查当前绑定表
// 定位动作 id——改键只动绑定表与注册，handler 本身从不重建
fn on_global_shortcut(app: &tauri::AppHandle, shortcut: &Shortcut, event: ShortcutEvent) {
    if event.state() != ShortcutState::Pressed {
        return;
    }
    let Some(state) = app.try_state::<ShortcutStore>() else {
        return;
    };
    let action = {
        let st = state.0.lock().unwrap();
        st.bindings
            .iter()
            .find(|(_, combo)| {
                combo.parse::<Shortcut>().is_ok_and(|s| same_shortcut(&s, shortcut))
            })
            .map(|(id, _)| id.clone())
    };
    if let Some(action) = action {
        dispatch_shortcut(app, &action);
    }
}

// 设置页拉取全绑定表（id → 键位串）+ 注册失败集合（failed 内动作页内标橙「被占用」）
#[tauri::command]
fn get_shortcuts(state: tauri::State<ShortcutStore>) -> ShortcutTable {
    state.0.lock().unwrap().clone()
}

// 改键：校验动作 id 与键位串 → 目标键已被其他动作占用则拒绝（Err 带占用者 id
// 供前端冲突提示）→ 注销旧键注册新键——注册失败（如系统保留键）回滚旧键并报错，
// 成功才落盘：内存绑定表与 shortcuts.json 与实际注册状态三者始终一致
#[tauri::command]
fn update_shortcut(
    app: tauri::AppHandle,
    state: tauri::State<ShortcutStore>,
    action: String,
    combo: String,
) -> Result<(), String> {
    if !SHORTCUT_DEFAULTS.iter().any(|(id, _)| *id == action) {
        return Err(format!("未知动作: {action}"));
    }
    let new: Shortcut = combo.parse().map_err(|e| format!("非法键位串: {e}"))?;
    let old = {
        let st = state.0.lock().unwrap();
        if let Some((occupied, _)) = st.bindings.iter().find(|(id, c)| {
            *id != &action && c.parse::<Shortcut>().is_ok_and(|s| same_shortcut(&s, &new))
        }) {
            return Err(format!("该键已被「{occupied}」占用"));
        }
        st.bindings.get(&action).cloned()
    };
    let old_shortcut = old.and_then(|c| c.parse::<Shortcut>().ok());
    if let Some(s) = old_shortcut.clone() {
        unregister_binding(&app, s);
    }
    if let Err(e) = register_binding(&app, new) {
        // 注册失败回滚旧键（best-effort，保持绑定表与注册状态一致）
        if let Some(s) = old_shortcut {
            let _ = app.global_shortcut().register(s);
        }
        return Err(e);
    }
    let bindings = {
        let mut st = state.0.lock().unwrap();
        // 新键注册成功：绑定表更新并摘除「被占用」标（旧键曾被抢占也不影响新键生效）
        st.failed.remove(&action);
        st.bindings.insert(action, combo);
        st.bindings.clone()
    };
    write_shortcut_bindings(&app, &bindings)
}

// 恢复默认：注销全部当前键 → 默认键逐条重注册（个别失败记入 failed 集合——可能被
// 其他应用占用，其余照常可用，页内标橙引导改键）→ 绑定表整体替换默认并落盘
#[tauri::command]
fn reset_shortcuts(app: tauri::AppHandle, state: tauri::State<ShortcutStore>) -> ShortcutTable {
    let old: Vec<Shortcut> = {
        let st = state.0.lock().unwrap();
        st.bindings.values().filter_map(|c| c.parse::<Shortcut>().ok()).collect()
    };
    for s in old {
        unregister_binding(&app, s);
    }
    let mut defaults = HashMap::new();
    let mut failed = std::collections::HashSet::new();
    for (id, c) in SHORTCUT_DEFAULTS {
        if let Ok(s) = c.parse::<Shortcut>() {
            if let Err(e) = app.global_shortcut().register(s) {
                eprintln!("注册默认快捷键 {id}（{c}）失败——该组合已被其他应用抢占，请到「设置-快捷键」改键: {e}");
                failed.insert((*id).to_string());
            }
        }
        defaults.insert((*id).to_string(), (*c).to_string());
    }
    let new_state = ShortcutTable { bindings: defaults.clone(), failed };
    *state.0.lock().unwrap() = new_state.clone();
    if let Err(e) = write_shortcut_bindings(&app, &defaults) {
        eprintln!("{e}");
    }
    new_state
}

// —— 主窗口几何防抖落盘：覆盖任务管理器强杀（TerminateProcess 无任何关闭事件，
// CloseRequested/RunEvent::Exit 都跑不到）场景。generation 计数器——每次几何事件
// +1 并 spawn 延时任务，醒来时仍是最新代号才写盘（旧任务自然过期退出，无需 abort
// 无泄漏积累）；spawn 闭包内重取 state，避免生命周期问题 ——
struct WindowStateFlush(std::sync::atomic::AtomicU64);

fn debounce_save_window_state(app: &tauri::AppHandle) {
    let Some(state) = app.try_state::<WindowStateFlush>() else {
        return;
    };
    let gen = state.0.fetch_add(1, std::sync::atomic::Ordering::Relaxed) + 1;
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(800)).await;
        let Some(state) = app.try_state::<WindowStateFlush>() else {
            return;
        };
        if state.0.load(std::sync::atomic::Ordering::Relaxed) == gen {
            if let Err(e) = app.save_window_state(WINDOW_STATE_FLAGS) {
                eprintln!("防抖落盘主窗口状态失败: {e}");
            }
        }
    });
}

// 唤回主窗口：恢复 + 显示 + 聚焦三连（最小化/隐藏两种状态都能唤回）
fn show_main(app: &tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.unminimize();
        let _ = win.show();
        let _ = win.set_focus();
    }
}

// 桌面小部件显隐切换（托盘菜单项）：可见→隐藏、隐藏→显示（常驻复用 WebView 不
// 销毁）；翻转后落盘 widget.json——重启按记忆自动恢复显隐
fn toggle_widget(app: &tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("widget") {
        match win.is_visible() {
            Ok(true) => {
                let _ = win.hide();
                write_widget_visible(app, false);
            }
            _ => {
                let _ = win.show();
                write_widget_visible(app, true);
            }
        }
    }
}

// 把边车（及其全部子孙进程，含 PyInstaller onefile 解压出的服务子进程）圈进
// KILL_ON_JOB_CLOSE 的 Job Object：本进程无论正常退出还是被强杀/崩溃，OS 关闭
// 作业句柄时由内核杀死作业内全部进程，边车不残留；
// 用户手动启动的 uvicorn 未加入作业，完全不受影响。
#[cfg(target_os = "windows")]
fn confine_child_to_job(child_pid: u32) {
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        SetInformationJobObject,
    };
    use windows_sys::Win32::System::Threading::{
        OpenProcess, PROCESS_SET_QUOTA, PROCESS_TERMINATE,
    };

    unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() {
            eprintln!("创建 Job Object 失败，边车进程可能残留");
            return;
        }
        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const core::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        ) == 0
        {
            eprintln!("设置 Job Object KILL_ON_JOB_CLOSE 失败");
            return;
        }
        // 句柄带 PROCESS_TERMINATE：Job Object 亦可代替 Exit 回调终止边车
        let process = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, child_pid);
        if process.is_null() {
            eprintln!("OpenProcess({child_pid}) 失败，边车进程可能残留");
            return;
        }
        if AssignProcessToJobObject(job, process) == 0 {
            eprintln!("AssignProcessToJobObject 失败，边车进程可能残留");
        }
        // 故意不 CloseHandle：作业句柄留在本进程生命周期内，进程退出时
        // OS 自动关闭句柄并触发 KILL_ON_JOB_CLOSE
    }
}

#[cfg(not(target_os = "windows"))]
fn confine_child_to_job(_child_pid: u32) {}

// 验证端口上是否 Clara 后端：手写最小 HTTP/1.1 GET /api/health（不引 HTTP 库），
// 读首块响应判状态行 200 即认定——本机回环特定端口上其他服务恰对此路径返回
// 200 的概率可忽略（误判则复用一个非 Clara 服务，应用空数据，改回端口自愈）
fn is_clara_backend(mut stream: TcpStream) -> bool {
    let request = "GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n";
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    // 防占端口的程序接受连接后不响应而挂住（本机回环 2s 足够）
    let _ = stream.set_read_timeout(Some(std::time::Duration::from_secs(2)));
    let mut buf = [0u8; 128];
    match stream.read(&mut buf) {
        Ok(n) if n > 0 => String::from_utf8_lossy(&buf[..n]).starts_with("HTTP/1.1 200"),
        _ => false,
    }
}

// 打包版端口被其他程序占用时的系统对话框提示（应用继续启动——后端缺席界面空
// 数据，但设置页"服务端口"行走 Tauri 命令不依赖后端，用户可改端口后重启）
#[cfg(target_os = "windows")]
fn warn_port_conflict(port: u16) {
    use windows_sys::Win32::UI::WindowsAndMessaging::{MessageBoxW, MB_ICONWARNING, MB_OK};

    let text = format!(
        "端口 {port} 已被其他程序占用，无法启动后台服务。\n请在 设置 → 通用偏好 → 系统与启动 中修改服务端口后重启应用。"
    );
    let caption = "Clara 服务端口冲突";
    let to_wide = |s: &str| {
        s.encode_utf16().chain(std::iter::once(0)).collect::<Vec<u16>>()
    };
    unsafe {
        MessageBoxW(
            std::ptr::null_mut(),
            to_wide(&text).as_ptr(),
            to_wide(caption).as_ptr(),
            MB_OK | MB_ICONWARNING,
        );
    }
}

#[cfg(not(target_os = "windows"))]
fn warn_port_conflict(_port: u16) {}

// 启动（或重启后再次启动）Python 边车：spawn + Job Object 圈禁 + 句柄入
// SidecarChild 状态 + 终端输出转发。setup 首启与 restart_backend 命令共用。
// SidecarChild 已 manage 时改写内部值（manage 同类型两次会 panic）。
fn spawn_sidecar(app: &tauri::AppHandle, port: u16) -> Result<(), String> {
    // 这里的名称必须与 tauri.conf.json 中的 externalBin 名称完全一致；
    // --port N 传后端监听端口（读 Tauri 侧配置，与首启同源）
    let sidecar_command = app
        .shell()
        .sidecar("clara-backend")
        .map_err(|e| format!("定位边车可执行文件失败: {e}"))?
        .args(["--port".to_string(), port.to_string()]);

    let (mut rx, child) = sidecar_command
        .spawn()
        .map_err(|e| format!("启动 Python 边车失败: {e}"))?;
    confine_child_to_job(child.pid());
    if let Some(state) = app.try_state::<SidecarChild>() {
        *state.0.lock().unwrap() = Some(child);
    } else {
        app.manage(SidecarChild(Mutex::new(Some(child))));
    }

    // 监听 Python 进程的终端输出（用于在 Rust 控制台查看 Python 日志）
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    println!("Python Stdout: {}", String::from_utf8_lossy(&line));
                }
                CommandEvent::Stderr(line) => {
                    eprintln!("Python Stderr: {}", String::from_utf8_lossy(&line));
                }
                _ => {}
            }
        }
    });
    Ok(())
}

// 备份恢复的编排钩子（2026-11-06 批 A）：前端在后端落好 restore_staging 后
// 调用——杀本应用管理的边车、等端口释放、按（恢复后可能已变的）配置端口
// 重新拉起；新进程启动时 Python 侧 lifespan 应用 staging 完成数据替换。
// dev 形态后端是终端手动 uvicorn（宿主无句柄无从杀起）/ 复用外部实例同拒，
// 均提示用户手动重启——恢复数据本身已就绪，只差进程重启。
#[tauri::command]
fn restart_backend(app: tauri::AppHandle) -> Result<(), String> {
    if cfg!(debug_assertions) {
        return Err("开发形态下后端由终端手动管理，请手动重启 uvicorn 进程".to_string());
    }
    let state = app.state::<SidecarChild>();
    let existing = state.0.lock().unwrap().take();
    let Some(child) = existing else {
        return Err("后端进程非本应用管理（可能复用了外部实例），请重启应用".to_string());
    };
    child
        .kill()
        .map_err(|e| format!("终止后端进程失败: {e}"))?;

    // kill 异步生效：轮询端口释放再重启（PyInstaller bootloader 与其运行时
    // 子进程退场有先后；8s 未释放视为异常，交给用户手动重启兜底）
    let port = read_server_port(&app);
    for _ in 0..40 {
        if TcpStream::connect(("127.0.0.1", port)).is_err() {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(200));
    }
    spawn_sidecar(&app, port)
}

// 桌面小部件的窗口玻璃（Win32 直调，现仅剩 DWM 圆角）。窗口级合成路线已在用户机器
// 上三次实测证伪、全线放弃：① window-vibrancy 0.6 acrylic（DWMWA_SYSTEMBACKDROP_TYPE）
// 窗口失焦退化为浅灰 fallback（系统规范不可关）；② accent 4（ACRYLICBLURBEHIND）
// 非激活窗口不渲染，重刷 SetWindowCompositionAttribute 也救不回（恒灰）；③ accent 3
// （ACCENT_ENABLE_BLURBEHIND）在 Win11 24H2 + WebView2 DirectComposition 内容窗上
// 直接渲成不透明灰板（既不模糊也不透视）。另有硬约束：Chromium 的 backdrop-filter
// 安全模型禁止采样窗口后的桌面，页内也做不出壁纸模糊。故定稿"纯透明直透"路线——
// 窗口 transparent 直透壁纸/应用界面，明暗观感与可读性全交前端 CSS tint 层
// （--glass-widget 亮暗两态），本模块只负责 DWM 窗口圆角
#[cfg(target_os = "windows")]
mod widget_glass {
    use windows_sys::Win32::Foundation::HWND;
    use windows_sys::Win32::Graphics::Dwm::DwmSetWindowAttribute;

    const DWMWA_WINDOW_CORNER_PREFERENCE: u32 = 33;
    const DWMWCP_ROUND: u32 = 2;

    // DWM 系统圆角（Win11；Win10 无此属性调用无效果，保持方角）：把窗口矩形四角裁成
    // 圆角，对齐 .widget-shell 的 border-radius:8px，防角部露出"透明直角圈"
    pub(super) unsafe fn round_corners(hwnd: HWND) {
        let pref = DWMWCP_ROUND;
        DwmSetWindowAttribute(
            hwnd,
            DWMWA_WINDOW_CORNER_PREFERENCE,
            &pref as *const u32 as *const core::ffi::c_void,
            std::mem::size_of::<u32>() as u32,
        );
    }
}

// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/
#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! You've been greeted from Rust!", name)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // 注册 shell 插件，用于拉起边车
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_opener::init())
        // 开机自启插件（注册表 Run 键由前端经 plugin-autostart JS API 落盘）
        .plugin(tauri_plugin_autostart::Builder::new().build())
        // 系统通知插件（"通知形式 = 系统通知"时日程提醒的呈现通道）
        .plugin(tauri_plugin_notification::init())
        // 系统对话框插件（数据接入页"本地文件夹"的目录选择）
        .plugin(tauri_plugin_dialog::init())
        // 窗口状态记忆（大小/位置/最大化：启动自动恢复、退出自动落盘）：
        // flags 不含 VISIBLE——主窗口常"隐藏到托盘"后退出，保存可见性会让下次
        // 启动窗口不显示；reminder 提醒窗由 JS 每次弹出前动态定位、widget 小部件
        // 位置由 JS localStorage 管理（且插件记忆 SIZE 会干扰固定尺寸），两者
        // denylist 彻底排除（不注册监听、不进状态文件）
        .plugin(
            tauri_plugin_window_state::Builder::default()
                .with_state_flags(WINDOW_STATE_FLAGS)
                .with_denylist(&["reminder", "widget"])
                .build(),
        )
        // 全局快捷键：handler 只注册一次，按键时反查 ShortcutStore 绑定表定位动作
        //（改键不动 handler）；实际按键注册在 setup 里读 shortcuts.json 逐条进行
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(on_global_shortcut)
                .build(),
        )
        .invoke_handler(tauri::generate_handler![
            greet,
            set_close_behavior,
            get_server_port,
            set_server_port,
            get_layout,
            set_layout,
            get_widget_visible,
            hide_widget,
            get_shortcuts,
            update_shortcut,
            reset_shortcuts,
            restart_backend
        ])
        // 关闭主窗口拦截："hide" 拦下关闭改为藏窗口（边车继续跑、托盘常驻）；
        // "quit" 放行销毁 → 既有 RunEvent::Exit kill 边车清理，零改动复用。
        // widget 小部件恒 hide（常驻辅助窗语义）——否则 close_action="quit" 时
        // Alt+F4 会真销毁小部件 webview，托盘再也唤不出，只能重启应用。
        // 主窗口关闭时手动落盘窗口状态：Windows 关机/session end（WM_CLOSE 后进程
        // 被杀）与 hide 到托盘后强杀都跑不到 RunEvent::Exit（quit 正常路径 Exit 兜底
        // 再存一次，幂等；denylist 窗口本就不进状态文件，save 只对 main 生效）
        .on_window_event(|window, event| {
            match event {
                tauri::WindowEvent::CloseRequested { api, .. } => {
                    if window.label() == "widget" {
                        api.prevent_close();
                        let _ = window.hide();
                        write_widget_visible(window.app_handle(), false);
                        return;
                    }
                    if window.label() == "main" {
                        let app = window.app_handle();
                        if let Err(e) = app.save_window_state(WINDOW_STATE_FLAGS) {
                            eprintln!("关闭时落盘主窗口状态失败: {e}");
                        }
                    }
                    // 关闭行为拦截对非 widget 窗口统一生效（reminder 的 Alt+F4 hide
                    // 拦截依赖此逻辑，不能因上面 gate main 而跳过）
                    let behavior = window
                        .app_handle()
                        .state::<CloseBehavior>()
                        .0
                        .lock()
                        .unwrap()
                        .clone();
                    if behavior == "hide" {
                        api.prevent_close();
                        let _ = window.hide();
                    }
                }
                // 主窗口几何变化防抖落盘（同样 gate main，防 widget/reminder 的
                // JS 层几何操作触发 Rust 事件空转）
                tauri::WindowEvent::Moved(_) | tauri::WindowEvent::Resized(_)
                    if window.label() == "main" =>
                {
                    debounce_save_window_state(window.app_handle());
                }
                _ => {}
            }
        })
        .setup(|app| {
            app.manage(CloseBehavior(Mutex::new("hide".to_string())));
            app.manage(WindowStateFlush(std::sync::atomic::AtomicU64::new(0)));

            // —— 全局快捷键：读绑定表（shortcuts.json 合并默认）逐条 best-effort 注册——
            // 个别失败记入 failed 集合（可能被其他应用占用/系统保留键，设置页标橙引导
            // 改键），其余照常可用；此段必须先于下方边车段的三处早退（dev 复用 8000 /
            // dev 后端未启动 / 打包版端口冲突），否则那三种启动形态下快捷键全丢
            let bindings = read_shortcut_bindings(app.handle());
            let mut failed = std::collections::HashSet::new();
            for (id, combo) in bindings.iter() {
                if let Ok(s) = combo.parse::<Shortcut>() {
                    if let Err(e) = app.handle().global_shortcut().register(s) {
                        eprintln!("注册全局快捷键 {id}（{combo}）失败——该组合已被其他应用抢占，请到「设置-快捷键」改键: {e}");
                        failed.insert(id.clone());
                    }
                }
            }
            app.manage(ShortcutStore(Mutex::new(ShortcutTable { bindings, failed })));

            // —— 桌面小部件：玻璃 = 窗口纯透明直透壁纸/应用界面 + 前端 CSS tint 层
            // （--glass-widget）；Rust 侧仅设 DWM 圆角（窗口级合成三次证伪的完整原因
            // 见 widget_glass 模块注释）——
            #[cfg(target_os = "windows")]
            if let Some(win) = app.get_webview_window("widget") {
                if let Ok(hwnd) = win.hwnd() {
                    unsafe {
                        widget_glass::round_corners(hwnd.0 as *mut core::ffi::c_void);
                    }
                }
            }

            // —— 系统托盘（常驻入口：右键菜单，左键单击唤回主窗口）——
            let show = MenuItem::with_id(app, "show", "显示主窗口", true, None::<&str>)?;
            let widget = MenuItem::with_id(app, "widget", "桌面小部件", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "退出 Clara", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &widget, &quit])?;
            let mut tray = TrayIconBuilder::with_id("main")
                .tooltip("Clara")
                .menu(&menu)
                // 左键单击不弹菜单（留给唤回主窗口），菜单走右键
                .show_menu_on_left_click(false);
            if let Some(icon) = app.default_window_icon() {
                tray = tray.icon(icon.clone());
            } else {
                eprintln!("缺少默认窗口图标，托盘使用系统占位图标");
            }
            tray.on_menu_event(|app, event| match event.id.as_ref() {
                "show" => show_main(app),
                "widget" => toggle_widget(app),
                "quit" => app.exit(0),
                _ => {}
            })
            .on_tray_icon_event(|tray, event| {
                if let TrayIconEvent::Click {
                    button: MouseButton::Left,
                    button_state: MouseButtonState::Up,
                    ..
                } = event
                {
                    show_main(tray.app_handle());
                }
            })
            .build(app)?;

            // —— 边车启动：dev 与打包版两套口径 ——
            // dev（debug build）：只复用固定 8000 的手动 uvicorn（终端 1，见 CLAUDE.md
            // 常用命令）；未启动则警告后不拉起——externalBin 恒是上次 build_backend.py
            // 的产物（可能严重落后源码，且 frozen 形态数据库落 %APPDATA%，与手动
            // uvicorn 的 backend/data 分裂成两套数据），静默拉起只会连到旧代码旧库
            // 打包版（release）：读配置端口 N，空闲则拉起；已有 Clara 后端（双开）则
            // 复用；被其他程序占用则弹窗提示改端口后重启（应用继续启动，不拉起）
            let (port, conflict) = if cfg!(debug_assertions) {
                if TcpStream::connect(("127.0.0.1", 8000)).is_ok() {
                    println!("127.0.0.1:8000 已有后端服务，复用（dev 形态）");
                    return Ok(());
                }
                eprintln!(
                    "开发模式：后端未启动。请先在终端 1 运行 uvicorn（见 CLAUDE.md 常用命令），再重启 npm run tauri dev"
                );
                return Ok(());
            } else {
                let port = read_server_port(app.handle());
                match TcpStream::connect(("127.0.0.1", port)) {
                    Ok(stream) => {
                        if is_clara_backend(stream) {
                            println!("127.0.0.1:{port} 已有 Clara 后端服务，跳过 sidecar 启动");
                            return Ok(());
                        }
                        (port, true) // 被其他程序占用：不拉起，弹提示后继续启动
                    }
                    Err(_) => (port, false),
                }
            };
            if conflict {
                warn_port_conflict(port);
                return Ok(());
            }

            // 这里的名称必须与 tauri.conf.json 中的 externalBin 名称完全一致；
            // spawn/圈禁/状态/日志转发收敛在 spawn_sidecar（restart_backend 共用）
            spawn_sidecar(app.handle(), port)?;

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while running tauri application")
        .run(|app, event| {
            // 应用退出时杀死边车进程，防止僵尸进程（未拉起边车时无操作）
            if let tauri::RunEvent::Exit = event {
                if let Some(state) = app.try_state::<SidecarChild>() {
                    if let Some(child) = state.0.lock().unwrap().take() {
                        if let Err(e) = child.kill() {
                            eprintln!("退出时杀死 Python 边车失败: {e}");
                        }
                    }
                }
            }
        });
}

#[cfg(test)]
mod shortcut_tests {
    use super::*;

    // 默认键串全部必须能被插件解析（定案 `[` `]` `\` 与数字的字面拼写），动作 id 不重复
    #[test]
    fn shortcut_defaults_parse() {
        let mut ids = std::collections::HashSet::new();
        for (id, combo) in SHORTCUT_DEFAULTS {
            assert!(combo.parse::<Shortcut>().is_ok(), "默认键串无法解析: {combo}");
            assert!(ids.insert(*id), "动作 id 重复: {id}");
        }
    }

    // 默认表内两两不得同键（表内冲突 = 后者永不可达）
    #[test]
    fn shortcut_defaults_no_conflict() {
        for (i, (_, a)) in SHORTCUT_DEFAULTS.iter().enumerate() {
            for (_, b) in &SHORTCUT_DEFAULTS[i + 1..] {
                let sa = a.parse::<Shortcut>().unwrap();
                let sb = b.parse::<Shortcut>().unwrap();
                assert!(!same_shortcut(&sa, &sb), "默认键重复: {a} / {b}");
            }
        }
    }
}
