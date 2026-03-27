"""
Gemini Computer Use - Multi-Agent Architecture
GKE Sandbox (Standard Pod, In-Cluster Mode)

架构：
  用户本地
    ↓ kubectl port-forward → Agent Pod (standard runc)
    ↓ HTTP POST /task
  Agent Pod（in-cluster，standard runc）
    ↓ Kubernetes API 创建 Chromium Sandbox Pod（sandbox namespace）
    ↓ 直连 Pod IP（in-cluster，无需 port-forward）
    ↓ Playwright connect_over_cdp() 连接 Sandbox
    ↓ Gemini Computer Use API 循环（动作执行 + 截图）
  GKE Sandbox Pod（chromium-sandbox，sandbox namespace）
    ↓ CDP WebSocket（直连 Pod IP）
  GCS（截图存储）

运行模式：
  SERVER_MODE=true   → FastAPI HTTP 服务器（部署在 K8s 中，用 port-forward 访问）
  SERVER_MODE=false  → 本地 CLI 交互模式（默认）

环境变量：
  GCP_PROJECT_ID          GCP 项目 ID（默认 hxhdemo）
  VERTEX_LOCATION         Vertex AI 地区（默认 global）
  GCS_RESULTS_BUCKET      截图存储 GCS Bucket
  MAIN_AGENT_MODEL        主 Agent 模型
  SUBAGENT_MODEL          Sub-Agent 编排模型
  COMPUTER_USE_MODEL      Computer Use 模型
  SANDBOX_NAMESPACE       K8s 命名空间（默认 sandbox）
  SANDBOX_IMAGE           Chromium Sandbox 镜像（默认 zenika/alpine-chrome:latest）
  SANDBOX_PORT            CDP 端口（默认 9222）
  SANDBOX_RUNTIME_CLASS   RuntimeClass（留空=standard runc，可设为 gvisor 等）
  IN_CLUSTER              自动检测（pod 内自动为 true，本地为 false）
  POD_READY_TIMEOUT       Pod 就绪超时秒数（默认 60）
  SERVER_MODE             true=HTTP 服务器模式，false=CLI 模式（默认 false）
  SERVER_PORT             HTTP 服务器端口（默认 8080）
"""

import asyncio
import base64 as _b64
import contextvars
import json as _json
import os
import socket
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any

# ── Google Cloud SDK ──────────────────────────────────────────────────────────
from google import genai
from google.genai import types
from google.cloud import storage

# ── Google ADK ────────────────────────────────────────────────────────────────
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part

# ── Kubernetes Python Client ───────────────────────────────────────────────────
from kubernetes import client as k8s_client, config as k8s_config

# ── Playwright (async) ────────────────────────────────────────────────────────
from playwright.async_api import async_playwright

# ── Requests（CDP 健康检查）──────────────────────────────────────────────────
import requests as http_requests

# ══════════════════════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════════════════════

GCP_PROJECT_ID     = os.environ.get("GCP_PROJECT_ID", "hxhdemo")
VERTEX_LOCATION    = os.environ.get("VERTEX_LOCATION", "global")
GCS_BUCKET         = os.environ.get("GCS_RESULTS_BUCKET", "hxhdemo-public")

# 模型
MAIN_AGENT_MODEL   = os.environ.get("MAIN_AGENT_MODEL",   "gemini-3-flash-preview")
SUBAGENT_MODEL     = os.environ.get("SUBAGENT_MODEL",     "gemini-3-flash-preview")
COMPUTER_USE_MODEL = os.environ.get("COMPUTER_USE_MODEL", "gemini-2.5-computer-use-preview-10-2025")

# GKE Sandbox
SANDBOX_NAMESPACE     = os.environ.get("SANDBOX_NAMESPACE", "sandbox")
SANDBOX_IMAGE         = os.environ.get("SANDBOX_IMAGE", "zenika/alpine-chrome:latest")
SANDBOX_PORT          = int(os.environ.get("SANDBOX_PORT", "9222"))
SANDBOX_RUNTIME_CLASS = os.environ.get("SANDBOX_RUNTIME_CLASS", "")  # 留空=standard runc

# 自动检测是否 in-cluster：Pod 内有 service account token
_IN_CLUSTER_AUTO = os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token")
IN_CLUSTER        = os.environ.get("IN_CLUSTER", "").lower() in ("1", "true", "yes") or _IN_CLUSTER_AUTO

POD_READY_TIMEOUT = int(os.environ.get("POD_READY_TIMEOUT", "60"))

# 服务器模式
SERVER_MODE = os.environ.get("SERVER_MODE", "false").lower() in ("1", "true", "yes")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8080"))

APP_NAME = "computer_use_multi_agent"

# ADK 使用 Vertex AI 后端
os.environ.setdefault("GOOGLE_CLOUD_PROJECT",      GCP_PROJECT_ID)
os.environ.setdefault("GOOGLE_CLOUD_LOCATION",     VERTEX_LOCATION)
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "1")


# ══════════════════════════════════════════════════════════════════════════════
# SSE 事件总线（通过 contextvars 传递，无需修改函数签名）
# ══════════════════════════════════════════════════════════════════════════════

# 每个 async task 持有自己的 Queue，通过 contextvar 传递，互不干扰
_task_event_queue: contextvars.ContextVar[Optional[asyncio.Queue]] = \
    contextvars.ContextVar("_task_event_queue", default=None)


async def _emit(event_type: str, data: Any) -> None:
    """向当前任务的 SSE 队列推送一条事件（若无队列则静默忽略）"""
    q = _task_event_queue.get()
    if q is not None:
        try:
            q.put_nowait({"type": event_type, "data": data})
        except asyncio.QueueFull:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# Kubernetes — Sandbox Pod 生命周期管理
# ══════════════════════════════════════════════════════════════════════════════

def _init_k8s():
    """初始化 K8s 客户端（in-cluster 或本地 kubeconfig）"""
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def create_sandbox_pod(task_id: str, runtime_class: str = "") -> str:
    """
    创建 Chromium Sandbox Pod（sandbox namespace）。
    runtime_class 优先使用参数值，为空时 fallback 到环境变量 SANDBOX_RUNTIME_CLASS。
    返回 pod_name。
    """
    _init_k8s()
    v1 = k8s_client.CoreV1Api()
    pod_name = f"sandbox-{task_id[:8]}"
    # 参数优先，空时 fallback 到全局默认
    effective_rc = runtime_class if runtime_class else SANDBOX_RUNTIME_CLASS

    pod = k8s_client.V1Pod(
        metadata=k8s_client.V1ObjectMeta(
            name=pod_name,
            namespace=SANDBOX_NAMESPACE,
            labels={
                "app":     "chromium-sandbox",
                "task-id": task_id,
            },
        ),
        spec=k8s_client.V1PodSpec(
            # RuntimeClass 可选：留空=standard runc，"gvisor"=gVisor 等
            runtime_class_name=effective_rc if effective_rc else None,
            restart_policy="Never",
            containers=[
                k8s_client.V1Container(
                    name="chromium",
                    image=SANDBOX_IMAGE,
                    ports=[k8s_client.V1ContainerPort(container_port=SANDBOX_PORT)],
                    # zenika/alpine-chrome entrypoint = chromium-browser
                    args=[
                        "--headless",                          # zenika/alpine-chrome 用旧式 headless（--headless=new 仅 Chrome 112+ 支持）
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--remote-debugging-address=0.0.0.0",
                        f"--remote-debugging-port={SANDBOX_PORT}",
                        "--window-size=1280,800",
                        # ── 降低 bot 检测概率 ──────────────────────────────
                        "--disable-blink-features=AutomationControlled",  # 隐藏 navigator.webdriver
                        "--disable-infobars",
                        "--lang=zh-CN,zh",
                        (
                            "--user-agent=Mozilla/5.0 (X11; Linux x86_64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                    ],
                    resources=k8s_client.V1ResourceRequirements(
                        requests={"memory": "512Mi", "cpu": "250m"},
                        limits=  {"memory": "1Gi",  "cpu": "1"},
                    ),
                )
            ],
        ),
    )

    v1.create_namespaced_pod(namespace=SANDBOX_NAMESPACE, body=pod)
    print(f"  [🐳 K8s] Sandbox Pod 创建: {SANDBOX_NAMESPACE}/{pod_name}")
    return pod_name


def wait_for_sandbox_ready(pod_name: str, timeout: int = POD_READY_TIMEOUT) -> str:
    """轮询 Pod 状态，直到 Running，返回 Pod IP"""
    _init_k8s()
    v1 = k8s_client.CoreV1Api()
    deadline = time.time() + timeout

    while time.time() < deadline:
        pod = v1.read_namespaced_pod(name=pod_name, namespace=SANDBOX_NAMESPACE)
        phase  = pod.status.phase
        pod_ip = pod.status.pod_ip

        if phase == "Running" and pod_ip:
            print(f"  [🐳 K8s] Pod Running ✓  IP: {pod_ip}")
            return pod_ip

        if phase in ("Failed", "Unknown"):
            raise RuntimeError(f"Sandbox Pod {pod_name} 异常状态: {phase}")

        elapsed = int(timeout - (deadline - time.time()))
        print(f"  [🐳 K8s] Pod 状态: {phase or 'Pending'}  ({elapsed}s)")
        time.sleep(3)

    raise TimeoutError(f"Sandbox Pod {pod_name} 未在 {timeout}s 内就绪")


def delete_sandbox_pod(pod_name: str):
    """任务结束后立即删除 Sandbox Pod"""
    try:
        _init_k8s()
        v1 = k8s_client.CoreV1Api()
        v1.delete_namespaced_pod(
            name=pod_name,
            namespace=SANDBOX_NAMESPACE,
            body=k8s_client.V1DeleteOptions(grace_period_seconds=0),
        )
        print(f"  [🐳 K8s] Sandbox Pod 已删除: {pod_name}")
    except Exception as e:
        print(f"  [⚠️ K8s] 删除 Pod 失败（可手动清理）: {e}")


def wait_for_cdp(cdp_url: str, timeout: int = 30):
    """等待 Chromium CDP HTTP 端点就绪"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = http_requests.get(f"{cdp_url}/json/version", timeout=2)
            if r.status_code == 200:
                print(f"  [🌐 CDP] Chromium 就绪 ✓  {cdp_url}")
                return
        except Exception:
            pass
        time.sleep(1)
    raise TimeoutError(f"CDP {cdp_url} 未在 {timeout}s 内响应")


@asynccontextmanager
async def sandbox_context(task_id: str, runtime_class: str = ""):
    """
    Async Context Manager：
    - in-cluster：直连 Pod IP（agent pod 部署在 K8s 内）
    - 本地：kubectl port-forward（sandbox pod 需为 standard runc）
    """
    effective_rc = runtime_class if runtime_class else SANDBOX_RUNTIME_CLASS
    rc_label = effective_rc or "standard (runc)"
    await _emit("log", f"🐳 正在创建 Sandbox Pod... [RuntimeClass: {rc_label}]")
    pod_name = create_sandbox_pod(task_id, runtime_class=runtime_class)
    pf_proc: Optional[subprocess.Popen] = None

    try:
        await _emit("log", f"⏳ 等待 Pod 就绪: {pod_name}")
        pod_ip = wait_for_sandbox_ready(pod_name)
        await _emit("log", f"✅ Sandbox Pod 就绪，IP: {pod_ip}")

        if IN_CLUSTER:
            cdp_url = f"http://{pod_ip}:{SANDBOX_PORT}"
            print(f"  [🔌 In-Cluster] 直连 {cdp_url}")
            await _emit("log", f"🔌 In-Cluster 直连 {cdp_url}")
        else:
            local_port = _free_port()
            pf_proc = subprocess.Popen(
                [
                    "kubectl", "port-forward",
                    f"pod/{pod_name}",
                    f"{local_port}:{SANDBOX_PORT}",
                    "-n", SANDBOX_NAMESPACE,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            await asyncio.sleep(3)
            if pf_proc.poll() is not None:
                err = pf_proc.stderr.read().decode() if pf_proc.stderr else ""
                raise RuntimeError(f"kubectl port-forward 启动失败: {err}")
            print(f"  [🔌 port-forward] localhost:{local_port} → pod:{SANDBOX_PORT}")
            await _emit("log", f"🔌 port-forward localhost:{local_port} → pod:{SANDBOX_PORT}")
            cdp_url = f"http://localhost:{local_port}"

        wait_for_cdp(cdp_url, timeout=60)
        await _emit("log", "🌐 Chromium CDP 就绪 ✓")
        yield cdp_url

    finally:
        if pf_proc:
            pf_proc.terminate()
        await _emit("log", f"🗑️ 清理 Sandbox Pod: {pod_name}")
        delete_sandbox_pod(pod_name)


# ══════════════════════════════════════════════════════════════════════════════
# GCS 截图上传
# ══════════════════════════════════════════════════════════════════════════════

def upload_screenshot(screenshot: bytes, gcs_path: str):
    if not GCS_BUCKET or not screenshot:
        return
    try:
        client = storage.Client(project=GCP_PROJECT_ID)
        client.bucket(GCS_BUCKET).blob(gcs_path).upload_from_string(
            screenshot, content_type="image/png"
        )
        print(f"  [☁️ GCS] gs://{GCS_BUCKET}/{gcs_path}")
    except Exception as e:
        print(f"  [⚠️ GCS] 上传失败: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Computer Use 核心循环
# ══════════════════════════════════════════════════════════════════════════════

def _has_function_calls(response) -> bool:
    if not response.candidates or not response.candidates[0].content.parts:
        return False
    return any(
        hasattr(p, "function_call") and p.function_call
        for p in response.candidates[0].content.parts
    )


def _get_px(coord: float, max_val: int) -> int:
    return max(0, min(int((coord / 1000.0) * max_val), max_val - 1))


async def _execute_actions(response, page, step_index: int, screenshots_prefix: str):
    function_response_parts = []
    viewport = await page.evaluate("() => ({width: window.innerWidth, height: window.innerHeight})")
    W, H = viewport["width"], viewport["height"]

    for part in response.candidates[0].content.parts:
        if not getattr(part, "function_call", None):
            continue

        call = part.function_call
        name = call.name
        args = call.args or {}
        print(f"  [🤖 Step {step_index:03d}] {name}({dict(args)})")
        await _emit("action", {"name": name, "args": dict(args), "step": step_index})
        await _emit("log", f"🤖 [{step_index:03d}] {name}({dict(args)})")

        try:
            if name == "wait_5_seconds":
                await asyncio.sleep(5)
            elif name == "go_back":
                await page.go_back()
            elif name == "go_forward":
                await page.go_forward()
            elif name == "navigate":
                await page.goto(args["url"], wait_until="domcontentloaded")
            elif name == "click_at":
                await page.mouse.click(
                    _get_px(args.get("x", 0), W),
                    _get_px(args.get("y", 0), H),
                )
            elif name == "hover_at":
                await page.mouse.move(
                    _get_px(args.get("x", 0), W),
                    _get_px(args.get("y", 0), H),
                )
            elif name == "type_text_at":
                x = _get_px(args.get("x", 0), W)
                y = _get_px(args.get("y", 0), H)
                await page.mouse.click(x, y)
                if args.get("clear_before_typing", True):
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Backspace")
                await page.keyboard.type(args.get("text", ""))
                if args.get("press_enter", True):
                    await page.keyboard.press("Enter")
            elif name == "key_combination":
                await page.keyboard.press(args.get("keys", ""))
            elif name == "scroll_document":
                delta = 800 if args.get("direction", "down") == "down" else -800
                await page.mouse.wheel(0, delta)
            elif name == "scroll_at":
                x = _get_px(args.get("x", 0), W)
                y = _get_px(args.get("y", 0), H)
                await page.mouse.move(x, y)
                mag = args.get("magnitude", 800)
                delta = mag if args.get("direction", "down") == "down" else -mag
                await page.mouse.wheel(0, delta)
            elif name == "drag_and_drop":
                x  = _get_px(args.get("x", 0), W)
                y  = _get_px(args.get("y", 0), H)
                dx = _get_px(args.get("destination_x", 0), W)
                dy = _get_px(args.get("destination_y", 0), H)
                await page.mouse.move(x, y)
                await page.mouse.down()
                await page.mouse.move(dx, dy)
                await page.mouse.up()
            await asyncio.sleep(0.5)

        except Exception as e:
            print(f"  [❌ 动作执行错误] {e}")
            await _emit("log", f"❌ 动作执行错误: {e}")

        try:
            screenshot = await page.screenshot(timeout=10000, animations="disabled")
        except Exception as e:
            print(f"  [⚠️ 截图失败] {e}")
            await _emit("log", f"⚠️ 截图失败: {e}")
            screenshot = b""

        # ── SSE 推送截图（base64）──
        if screenshot:
            await _emit("screenshot", {
                "step": f"step-{step_index:03d}-{name}",
                "image": _b64.b64encode(screenshot).decode(),
            })

        await asyncio.to_thread(
            upload_screenshot,
            screenshot,
            f"{screenshots_prefix}/step-{step_index:03d}-{name}.png",
        )

        function_response_parts.append(
            types.Part(
                function_response=types.FunctionResponse(
                    name=name,
                    response={"url": page.url},
                    parts=[
                        types.FunctionResponsePart(
                            inline_data=types.FunctionResponseBlob(
                                mime_type="image/png",
                                data=screenshot,
                            )
                        )
                    ] if screenshot else [],
                )
            )
        )
        step_index += 1

    return types.Content(role="user", parts=function_response_parts), step_index


async def run_computer_use_loop(
    page,
    task_description: str,
    max_steps: int,
    screenshots_prefix: str,
) -> tuple[str, int]:
    genai_client = genai.Client(
        vertexai=True,
        project=GCP_PROJECT_ID,
        location=VERTEX_LOCATION,
    )
    config = types.GenerateContentConfig(
        temperature=0.0,
        system_instruction=(
            "你是一个浏览器自动化助手。使用提供的工具操作浏览器完成任务，"
            "完成后输出任务结果摘要。"
        ),
        tools=[
            types.Tool(
                computer_use=types.ComputerUse(
                    environment=types.Environment.ENVIRONMENT_BROWSER,
                )
            )
        ],
    )

    try:
        init_shot = await page.screenshot(timeout=10000, animations="disabled")
        await asyncio.to_thread(upload_screenshot, init_shot, f"{screenshots_prefix}/step-000-init.png")
        if init_shot:
            await _emit("screenshot", {
                "step": "step-000-init",
                "image": _b64.b64encode(init_shot).decode(),
            })
    except Exception:
        init_shot = b""

    parts = [types.Part.from_text(text=task_description)]
    if init_shot:
        parts.append(types.Part.from_bytes(data=init_shot, mime_type="image/png"))
    contents = [types.Content(role="user", parts=parts)]

    step = 1
    final_summary = ""

    while step <= max_steps:
        print(f"\n  🧠 Gemini 思考中... (Step {step}/{max_steps})")
        await _emit("step_start", {"step": step, "max_steps": max_steps})
        await _emit("log", f"🧠 Gemini 思考中... (Step {step}/{max_steps})")

        try:
            response = await asyncio.to_thread(
                genai_client.models.generate_content,
                model=COMPUTER_USE_MODEL,
                contents=contents,
                config=config,
            )
        except Exception as e:
            print(f"  ❌ Gemini API 错误: {e}")
            await _emit("log", f"❌ Gemini API 错误: {e}")
            break

        contents.append(response.candidates[0].content)

        for part in response.candidates[0].content.parts:
            if getattr(part, "text", None):
                text = part.text.strip()
                print(f"\n  💡 Gemini: {text}\n")
                await _emit("agent_text", text)
                await _emit("log", f"💡 Gemini: {text}")
                final_summary = text

        if not _has_function_calls(response):
            print(f"\n  ✅ 任务完成: {final_summary or '[完成]'}")
            await _emit("log", f"✅ 任务完成: {(final_summary or '[完成]')[:80]}")
            break

        feedback, step = await _execute_actions(response, page, step, screenshots_prefix)
        contents.append(feedback)

    else:
        msg = f"⚠️ 已达最大步骤数 ({max_steps})，强制终止"
        print(f"\n  {msg}")
        await _emit("log", msg)

    return final_summary, step - 1


# ══════════════════════════════════════════════════════════════════════════════
# Computer Use 工具函数（ADK Sub-Agent 和 HTTP API 共用）
# ══════════════════════════════════════════════════════════════════════════════

async def run_computer_use_task(
    task_description: str,
    starting_url: str = "https://www.google.com",
    max_steps: int = 20,
    runtime_class: str = "",
) -> dict:
    """
    Computer Use 核心工具：
    1. 在 sandbox namespace 创建 Chromium Sandbox Pod
    2. in-cluster 直连 Pod IP（无需 port-forward）
    3. Playwright connect_over_cdp() + Gemini Computer Use API 循环
    4. 截图上传 GCS，任务完成后删除 Pod
    runtime_class: "" | "gvisor" | "kata-qemu" | "kata-fc"，留空使用环境变量默认值
    """
    task_id            = str(uuid.uuid4())
    screenshots_prefix = f"screenshots/{task_id}"
    effective_rc = runtime_class if runtime_class else SANDBOX_RUNTIME_CLASS

    print(f"\n{'─'*60}")
    print(f"🚀 [ComputerUse] 任务启动")
    print(f"   Task ID      : {task_id}")
    print(f"   描述         : {task_description}")
    print(f"   URL          : {starting_url}")
    print(f"   Max Steps    : {max_steps}")
    print(f"   模式         : {'In-Cluster（直连 Pod IP）' if IN_CLUSTER else 'Local（port-forward）'}")
    print(f"   RuntimeClass : {effective_rc or 'standard (runc)'}")
    print(f"   截图         : gs://{GCS_BUCKET}/{screenshots_prefix}/")
    print(f"{'─'*60}")

    await _emit("log", f"🚀 任务启动: {task_description[:60]}{'...' if len(task_description) > 60 else ''}")
    await _emit("log", f"📋 Task ID: {task_id}")
    await _emit("log", f"🌐 起始 URL: {starting_url}  最大步数: {max_steps}")
    await _emit("log", f"⚙️  RuntimeClass: {effective_rc or 'standard (runc)'}  In-Cluster: {IN_CLUSTER}")

    try:
        async with sandbox_context(task_id, runtime_class=runtime_class) as cdp_url:
            async with async_playwright() as p:
                await _emit("log", "🎭 Playwright 连接 Chromium...")
                browser = await p.chromium.connect_over_cdp(cdp_url)
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                # 注入反检测脚本：覆盖 navigator.webdriver，每个新页面都会执行
                await context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    window.chrome = { runtime: {} };
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
                """)
                page    = context.pages[0]    if context.pages    else await context.new_page()
                await page.set_viewport_size({"width": 1280, "height": 800})

                if starting_url and starting_url != "about:blank":
                    print(f"\n  🌐 导航: {starting_url}")
                    await _emit("log", f"🌐 导航到: {starting_url}")
                    await page.goto(starting_url, wait_until="domcontentloaded", timeout=20000)

                summary, total_steps = await run_computer_use_loop(
                    page=page,
                    task_description=task_description,
                    max_steps=max_steps,
                    screenshots_prefix=screenshots_prefix,
                )

                try:
                    final_shot = await page.screenshot(timeout=10000, animations="disabled")
                    await asyncio.to_thread(upload_screenshot, final_shot, f"{screenshots_prefix}/final.png")
                    if final_shot:
                        await _emit("screenshot", {
                            "step": "final",
                            "image": _b64.b64encode(final_shot).decode(),
                        })
                except Exception as e:
                    print(f"  [⚠️] 最终截图失败: {e}")

        gcs_screenshots = f"gs://{GCS_BUCKET}/{screenshots_prefix}/" if GCS_BUCKET else None
        result = {
            "task_id":         task_id,
            "status":          "completed",
            "summary":         summary,
            "total_steps":     total_steps,
            "gcs_screenshots": gcs_screenshots,
        }
        print(f"\n✅ [ComputerUse] 完成！步骤: {total_steps}  摘要: {(summary or '')[:120]}")
        await _emit("log", f"🎉 任务完成！共 {total_steps} 步")
        if gcs_screenshots:
            await _emit("log", f"☁️ 截图已上传: {gcs_screenshots}")
        return result

    except Exception as e:
        error_msg = str(e)
        print(f"\n❌ [ComputerUse] 任务失败: {error_msg}")
        await _emit("log", f"❌ 任务失败: {error_msg}")
        return {
            "task_id": task_id,
            "status":  "failed",
            "error":   error_msg,
        }


# ══════════════════════════════════════════════════════════════════════════════
# ADK Multi-Agent 定义
# ══════════════════════════════════════════════════════════════════════════════

computer_use_sub_agent = LlmAgent(
    name="computer_use_subagent",
    model=SUBAGENT_MODEL,
    description=(
        "浏览器自动化执行 Agent：在 GKE Sandbox 中运行 Gemini Computer Use，"
        "完成具体的浏览器操作任务。"
    ),
    instruction=f"""你是一个浏览器自动化执行 Agent。

当收到浏览器自动化子任务时：
1. 调用 run_computer_use_task 工具执行任务
   - task_description：详细描述要完成的操作（尽量具体，包含目标和验收标准）
   - starting_url：任务起始页面
   - max_steps：根据任务复杂度估算（简单操作 5-10，复杂流程 15-25）
2. 将工具返回的结果整理后返回（摘要、截图路径、步骤数）

Sandbox 配置：
- Computer Use 模型：{COMPUTER_USE_MODEL}
- 截图存储：gs://{GCS_BUCKET}/screenshots/
- RuntimeClass：{SANDBOX_RUNTIME_CLASS or 'standard (runc)'}
""",
    tools=[run_computer_use_task],
)

root_agent = LlmAgent(
    name="main_agent",
    model=MAIN_AGENT_MODEL,
    description="主调度 Agent：理解用户意图，拆解复杂任务，将子任务委托给 Sub-Agent 执行。",
    instruction=f"""你是一个智能任务调度主 Agent。

**1. 理解任务**
- 分析用户的自然语言描述，提取核心目标

**2. 任务拆解**
- 简单任务：直接委托给 computer_use_subagent
- 复杂任务：拆解为多个独立子任务，按顺序委托，最后汇总

**3. 结果汇总**
- 清晰的中文汇总执行情况、关键发现、截图路径

配置：
- Sandbox：sandbox namespace（每个子任务独立隔离的 Chromium）
- 截图：gs://{GCS_BUCKET}/screenshots/
- Computer Use 模型：{COMPUTER_USE_MODEL}
""",
    sub_agents=[computer_use_sub_agent],
)


# ══════════════════════════════════════════════════════════════════════════════
# FastAPI 服务器模式（SERVER_MODE=true，部署到 K8s 后用 port-forward 访问）
# ══════════════════════════════════════════════════════════════════════════════

def build_fastapi_app():
    import pathlib
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel

    app = FastAPI(
        title="Computer Use Agent API",
        description="Gemini Computer Use Multi-Agent — HTTP API",
        version="1.0.0",
    )

    # 任务存储（内存，重启丢失）
    _tasks: Dict[str, Any] = {}
    # 每个任务的 SSE 事件队列
    _task_queues: Dict[str, asyncio.Queue] = {}

    class TaskRequest(BaseModel):
        task: str
        starting_url: str = "https://www.google.com"
        max_steps: int = 20
        runtime_class: str = ""  # "" | "gvisor" | "kata-qemu" | "kata-fc"，留空使用环境变量默认值

    # ── 静态文件 / Web UI ──────────────────────────────────────────────────
    static_dir = pathlib.Path(__file__).parent / "static"

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
    async def serve_ui():
        html_file = static_dir / "index.html"
        if not html_file.exists():
            return HTMLResponse("<h2>Web UI not found. Please create static/index.html</h2>", status_code=404)
        return HTMLResponse(html_file.read_text(encoding="utf-8"))

    # ── 健康检查 ────────────────────────────────────────────────────────────
    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "in_cluster": IN_CLUSTER,
            "sandbox_namespace": SANDBOX_NAMESPACE,
            "sandbox_image": SANDBOX_IMAGE,
            "sandbox_runtime_class": SANDBOX_RUNTIME_CLASS or "standard (runc)",
            "computer_use_model": COMPUTER_USE_MODEL,
        }

    # ── 任务提交（异步）────────────────────────────────────────────────────
    @app.post("/task")
    async def submit_task(req: TaskRequest):
        """提交任务（异步后台执行，立即返回 task_id）"""
        task_id = str(uuid.uuid4())
        queue: asyncio.Queue = asyncio.Queue()
        _tasks[task_id] = {
            "task_id": task_id,
            "status": "pending",
            "task": req.task,
            "starting_url": req.starting_url,
            "max_steps": req.max_steps,
        }
        _task_queues[task_id] = queue

        # 在创建 task 前设置 contextvar，create_task 会拷贝当前 context
        token = _task_event_queue.set(queue)

        async def _run():
            _tasks[task_id]["status"] = "running"
            result = await run_computer_use_task(req.task, req.starting_url, req.max_steps, runtime_class=req.runtime_class)
            _tasks[task_id].update(result)
            # 推送 done 事件，SSE 消费后关闭
            await queue.put({"type": "done", "data": result})
            # 1 分钟后清理队列
            await asyncio.sleep(60)
            _task_queues.pop(task_id, None)

        asyncio.create_task(_run())
        _task_event_queue.reset(token)

        return {"task_id": task_id, "status": "pending"}

    # ── 任务提交（同步）────────────────────────────────────────────────────
    @app.post("/task/sync")
    async def submit_task_sync(req: TaskRequest):
        """提交任务（同步，等待完成后返回结果）"""
        return await run_computer_use_task(req.task, req.starting_url, req.max_steps, runtime_class=req.runtime_class)

    # ── SSE：实时事件流 ─────────────────────────────────────────────────────
    @app.get("/task/{task_id}/events")
    async def task_events(task_id: str):
        """SSE 端点：实时推送任务日志、截图、Agent 文本"""
        queue = _task_queues.get(task_id)
        if queue is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id} 不存在或事件流已关闭")

        async def generate():
            # 握手
            yield f"data: {_json.dumps({'type': 'connected', 'data': task_id}, ensure_ascii=False)}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"data: {_json.dumps(event, ensure_ascii=False)}\n\n"
                    if event.get("type") in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    # keepalive 注释行，防止代理断开
                    yield ": keepalive\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ── 任务状态查询 ────────────────────────────────────────────────────────
    @app.get("/task/{task_id}")
    def get_task(task_id: str):
        """查询任务状态和结果"""
        if task_id not in _tasks:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        return _tasks[task_id]

    @app.get("/tasks")
    def list_tasks():
        """列出所有任务"""
        return list(_tasks.values())

    @app.delete("/task/{task_id}")
    def delete_task(task_id: str):
        """删除任务记录"""
        if task_id not in _tasks:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        _tasks.pop(task_id)
        _task_queues.pop(task_id, None)
        return {"deleted": task_id}

    return app


# ══════════════════════════════════════════════════════════════════════════════
# CLI 模式（本地交互）
# ══════════════════════════════════════════════════════════════════════════════

async def run_cli():
    print("=" * 65)
    print("🤖 Gemini Computer Use - Multi-Agent + GKE Sandbox")
    print(f"   Main Agent    : {MAIN_AGENT_MODEL}")
    print(f"   Sub-Agent     : {SUBAGENT_MODEL}")
    print(f"   Computer Use  : {COMPUTER_USE_MODEL}")
    print(f"   Sandbox NS    : {SANDBOX_NAMESPACE}")
    print(f"   Sandbox Image : {SANDBOX_IMAGE}")
    print(f"   RuntimeClass  : {SANDBOX_RUNTIME_CLASS or 'standard (runc)'}")
    print(f"   GCS 截图      : gs://{GCS_BUCKET}/screenshots/")
    print(f"   运行模式      : {'In-Cluster（直连 Pod IP）' if IN_CLUSTER else 'Local（kubectl port-forward）'}")
    print("=" * 65)

    session_service = InMemorySessionService()
    session = await session_service.create_session(app_name=APP_NAME, user_id="user")
    runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)

    while True:
        try:
            user_input = input("\n👨‍💻 请输入任务: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 退出。")
            break

        if not user_input or user_input.lower() in ("exit", "quit"):
            print("👋 退出。")
            break

        message = Content(role="user", parts=[Part.from_text(text=user_input)])
        print("\n🧠 Main Agent 分析任务中...\n")

        try:
            async for event in runner.run_async(
                user_id="user",
                session_id=session.id,
                new_message=message,
            ):
                if event.get_function_calls():
                    for call in event.get_function_calls():
                        author = getattr(event, "author", "")
                        print(f"  🔧 [{author}] 工具调用: {call.name}")
                if event.is_final_response() and event.content:
                    for part in event.content.parts:
                        if hasattr(part, "text") and part.text:
                            print(f"\n✅ Agent 回复:\n{part.text.strip()}")
        except Exception as e:
            print(f"\n❌ 执行错误: {e}")
            import traceback
            traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════════════════

if SERVER_MODE:
    # uvicorn 作为 ASGI 服务器时直接加载此 app 对象
    app = build_fastapi_app()

if __name__ == "__main__":
    if SERVER_MODE:
        import uvicorn
        print("=" * 65)
        print("🌐 Computer Use Agent — HTTP Server Mode")
        print(f"   Port          : {SERVER_PORT}")
        print(f"   Sandbox NS    : {SANDBOX_NAMESPACE}")
        print(f"   Sandbox Image : {SANDBOX_IMAGE}")
        print(f"   RuntimeClass  : {SANDBOX_RUNTIME_CLASS or 'standard (runc)'}")
        print(f"   In-Cluster    : {IN_CLUSTER}")
        print(f"   GCS 截图      : gs://{GCS_BUCKET}/screenshots/")
        print("=" * 65)
        print(f"\n📖 API 文档: http://localhost:{SERVER_PORT}/docs")
        print(f"🖥️  Web  UI : http://localhost:{SERVER_PORT}/\n")
        # 注意：直接传 app 对象（不用字符串），避免 Python 无法 import 含连字符的模块名
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=SERVER_PORT,
            reload=False,
            log_level="info",
        )
    else:
        asyncio.run(run_cli())
