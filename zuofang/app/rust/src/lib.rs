//! JNI 入口。方法名与上游 APK 的 `NativeBridge` 完全一致：
//! `Java_com_zuofang_app_NativeBridge_{prepare,start,status,stop,captureStatus}`

mod config;
mod engine;
pub mod proto;
mod tun;

use jni::objects::{GlobalRef, JObject, JString, JValue};
use jni::sys::{jint, jstring};
use jni::{JNIEnv, JavaVM};

const BRIDGE: &str = "com/zuofang/app/NativeBridge";

fn to_jstring(env: &mut JNIEnv, s: String) -> jstring {
    match env.new_string(s) {
        Ok(js) => js.into_raw(),
        Err(_) => std::ptr::null_mut(),
    }
}

fn err_json(reason: &str) -> String {
    serde_json::json!({ "state": "error", "reason": reason }).to_string()
}

fn read_config(env: &mut JNIEnv, config: &JString) -> Result<String, String> {
    env.get_string(config)
        .map(|s| s.into())
        .map_err(|e| format!("配置读取失败: {e}"))
}

/// 通过 GlobalRef + JavaVM 调用 `ZfVpnService.protectSocket(int): Boolean`。
/// 只在 `start()` 的调用线程上同步执行一次（保护 UDP socket），不跨线程持有。
fn protect_via(vm: &JavaVM, svc: &GlobalRef, fd: jint) -> bool {
    let mut env = match vm.attach_current_thread() {
        Ok(e) => e,
        Err(_) => return false,
    };
    let r = env.call_method(
        svc.as_obj(),
        "protectSocket",
        "(I)Z",
        &[JValue::Int(fd)],
    );
    match r {
        Ok(v) => v.z().unwrap_or(false),
        Err(_) => false,
    }
}

#[no_mangle]
pub extern "system" fn Java_com_zuofang_app_NativeBridge_prepare(
    mut env: JNIEnv,
    _this: JObject,
    _service: JObject,
    config: JString,
) -> jstring {
    let raw = match read_config(&mut env, &config) {
        Ok(v) => v,
        Err(e) => return to_jstring(&mut env, err_json(&e)),
    };
    to_jstring(&mut env, engine::prepare(&raw))
}

#[no_mangle]
pub extern "system" fn Java_com_zuofang_app_NativeBridge_start(
    mut env: JNIEnv,
    _this: JObject,
    service: JObject,
    config: JString,
) -> jstring {
    let raw = match read_config(&mut env, &config) {
        Ok(v) => v,
        Err(e) => return to_jstring(&mut env, err_json(&e)),
    };

    // TUN fd 由 Kotlin 侧从 ParcelFileDescriptor 取出后暴露为 tunFd()
    let fd: jint = match env.call_method(&service, "tunFd", "()I", &[]) {
        Ok(v) => v.i().unwrap_or(-1),
        Err(_) => -1,
    };
    if fd < 0 {
        return to_jstring(&mut env, err_json("TUN descriptor required"));
    }

    let vm = match env.get_java_vm() {
        Ok(v) => v,
        Err(_) => return to_jstring(&mut env, err_json("无法获取 JavaVM")),
    };
    let svc = match env.new_global_ref(&service) {
        Ok(v) => v,
        Err(_) => return to_jstring(&mut env, err_json("无法持有 Service 引用")),
    };

    let protect = move |sock_fd: jint| protect_via(&vm, &svc, sock_fd);
    let out = engine::start(&raw, fd, &protect);
    to_jstring(&mut env, out)
}

#[no_mangle]
pub extern "system" fn Java_com_zuofang_app_NativeBridge_status(
    mut env: JNIEnv,
    _this: JObject,
) -> jstring {
    to_jstring(&mut env, engine::status())
}

#[no_mangle]
pub extern "system" fn Java_com_zuofang_app_NativeBridge_stop(
    mut env: JNIEnv,
    _this: JObject,
) -> jstring {
    to_jstring(&mut env, engine::stop())
}

#[no_mangle]
pub extern "system" fn Java_com_zuofang_app_NativeBridge_captureStatus(
    mut env: JNIEnv,
    _this: JObject,
) -> jstring {
    to_jstring(&mut env, engine::capture_status())
}

/// 供 `nm -D` 校验：确认 BRIDGE 常量参与了编译期检查。
#[no_mangle]
pub extern "system" fn Java_com_zuofang_app_NativeBridge_className(
    mut env: JNIEnv,
    _this: JObject,
) -> jstring {
    to_jstring(&mut env, BRIDGE.to_string())
}
