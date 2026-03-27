"""
Gemini Computer Use Agent - 使用指南

1. 环境准备:
   - 确保安装了 google-genai 和 playwright: pip install -U google-genai playwright
   - 确保安装了 playwright 浏览器驱动: playwright install chromium

2. 认证 (如果使用 Vertex AI):
   gcloud auth application-default login

3. 运行 Agent:
   python3 agent.py
   (脚本会自动启动带调试端口的 Chrome，无需手动操作)

4. 使用说明:
   - 脚本会自动连接到 9222 端口的 Chrome。
   - 在提示符下输入指令（如：打开 console.cloud.google.com 查看 VM）。
   - 输入 'exit' 或 'quit' 退出。
"""

import os
import time
import sys
import select
import subprocess
import signal
import requests
from google import genai
from google.genai import types
from playwright.sync_api import sync_playwright

# GCP 项目配置（从环境变量读取，支持自定义）
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "hxhdemo")
LOCATION = os.environ.get("VERTEX_LOCATION", "global")
MODEL_ID = os.environ.get("COMPUTER_USE_MODEL", "gemini-2.5-computer-use-preview-10-2025")

def has_function_calls(response):
    """检查模型响应中是否包含函数调用"""
    if not response.candidates or not response.candidates[0].content.parts:
        return False
    candidate = response.candidates[0]
    return any(hasattr(part, 'function_call') and part.function_call for part in candidate.content.parts)

def has_function_calls_in_history(contents):
    """辅助函数：检查最后一条模型消息是否包含了工具调用"""
    if not contents:
        return False
    last_content = contents[-1]
    if getattr(last_content, "role", None) == "user":
        return True 
    if not getattr(last_content, "parts", None):
        return False
    return any(hasattr(part, 'function_call') and part.function_call for part in last_content.parts)

def execute_and_create_feedback(response, page):
    """解析内置 computer_use 指令，执行并按照官方要求构建包含截图的 FunctionResponse"""
    function_response_parts = []
    candidate = response.candidates[0]
    
    # 每次动作前，通过 JS 动态获取当前真实的视口内部尺寸，避免 Retina/CDP 导致的偏移
    viewport = page.evaluate("() => { return {width: window.innerWidth, height: window.innerHeight}; }")
    screen_width = viewport['width']
    screen_height = viewport['height']
    # print(f"  [🔍 动态视口校准] {screen_width}x{screen_height}")

    def get_px(coord, max_val):
        # 增加边界保护，防止因为四舍五入超出屏幕
        val = int((coord / 1000.0) * max_val)
        return max(0, min(val, max_val - 1))

    for part in candidate.content.parts:
        if not getattr(part, 'function_call', None):
            continue
        
        call = part.function_call
        name = call.name
        args = call.args if call.args else {}
        
        print(f"  [🤖 动作执行] {name}({args})")
        
        try:
            if name == "open_web_browser":
                pass 
            elif name == "wait_5_seconds":
                time.sleep(5)
            elif name == "go_back":
                page.go_back()
            elif name == "go_forward":
                page.go_forward()
            elif name == "search":
                page.goto("https://www.google.com")
            elif name == "navigate":
                page.goto(args["url"])
            elif name == "click_at":
                x, y = get_px(args.get("x", 0), screen_width), get_px(args.get("y", 0), screen_height)
                page.mouse.click(x, y)
            elif name == "hover_at":
                x, y = get_px(args.get("x", 0), screen_width), get_px(args.get("y", 0), screen_height)
                page.mouse.move(x, y)
            elif name == "type_text_at":
                x, y = get_px(args.get("x", 0), screen_width), get_px(args.get("y", 0), screen_height)
                page.mouse.click(x, y)
                if args.get("clear_before_typing", True):
                    page.keyboard.press("Meta+A")
                    page.keyboard.press("Backspace")
                page.keyboard.type(args.get("text", ""))
                if args.get("press_enter", True):
                    page.keyboard.press("Enter")
            elif name == "key_combination":
                page.keyboard.press(args.get("keys", ""))
            elif name == "scroll_document":
                direction = args.get("direction", "down")
                if direction == "down": page.mouse.wheel(0, 800)
                elif direction == "up": page.mouse.wheel(0, -800)
                elif direction == "left": page.mouse.wheel(-800, 0)
                elif direction == "right": page.mouse.wheel(800, 0)
            elif name == "scroll_at":
                x, y = get_px(args.get("x", 0), screen_width), get_px(args.get("y", 0), screen_height)
                page.mouse.move(x, y)
                direction, mag = args.get("direction", "down"), args.get("magnitude", 800)
                if direction == "down": page.mouse.wheel(0, mag)
                elif direction == "up": page.mouse.wheel(0, -mag)
                elif direction == "left": page.mouse.wheel(-mag, 0)
                elif direction == "right": page.mouse.wheel(mag, 0)
            elif name == "drag_and_drop":
                x, y = get_px(args.get("x", 0), screen_width), get_px(args.get("y", 0), screen_height)
                dx, dy = get_px(args.get("destination_x", 0), screen_width), get_px(args.get("destination_y", 0), screen_height)
                page.mouse.move(x, y)
                page.mouse.down()
                page.mouse.move(dx, dy)
                page.mouse.up()
            
            # 等待一小会儿确保动作生效
            time.sleep(1)

        except Exception as e:
            print(f"  [❌ 执行错误] {e}")

        # 每次动作执行后（或捕获到错误后），进行截图并获取当前 URL
        current_url = page.url
        try:
            screenshot = page.screenshot(timeout=10000, animations="disabled")
        except Exception as e:
            print(f"  [⚠️ 截图失败]: {e}")
            screenshot = b""

        # 按照官方 Computer Use 模型的特殊要求构建带有截图的 FunctionResponse
        response_part = types.FunctionResponse(
            name=name,
            response={"url": current_url},
            parts=[
                types.FunctionResponsePart(
                    inline_data=types.FunctionResponseBlob(
                        mime_type="image/png",
                        data=screenshot
                    )
                )
            ] if screenshot else []
        )
        # 注意: genai SDK 的 Part 可以包装 FunctionResponse
        function_response_parts.append(types.Part(function_response=response_part))

    # 返回这一轮所有动作的反馈集合
    return types.Content(role="user", parts=function_response_parts)
# Chrome 配置（从环境变量读取，支持自定义）
CHROME_PATH = os.environ.get("CHROME_PATH", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
CHROME_DEBUG_PORT = int(os.environ.get("CHROME_DEBUG_PORT", "9222"))
CHROME_USER_DATA_DIR = os.environ.get("CHROME_USER_DATA_DIR", os.path.expanduser("~/Library/Application Support/Google/ChromeDebug"))

def is_chrome_debug_running(port=CHROME_DEBUG_PORT):
    """检查指定端口的 Chrome 调试端口是否已在运行"""
    try:
        resp = requests.get(f"http://localhost:{port}/json/version", timeout=2)
        return resp.status_code == 200
    except Exception:
        return False

def launch_chrome_with_debug_port():
    """
    自动以调试模式启动 Chrome。
    - 如果 Chrome 已在 9222 端口监听，直接复用，不重复启动。
    - 否则启动新的 Chrome 进程（后台运行），等待其就绪后返回进程对象。
    """
    if is_chrome_debug_running():
        print("✅ 检测到 Chrome 已在调试端口运行，直接复用。")
        return None  # 不需要管理进程生命周期

    print("🌐 正在启动 Chrome（调试端口 9222）...")
    os.makedirs(CHROME_USER_DATA_DIR, exist_ok=True)
    proc = subprocess.Popen(
        [
            CHROME_PATH,
            f"--remote-debugging-port={CHROME_DEBUG_PORT}",
            f"--user-data-dir={CHROME_USER_DATA_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # 等待 Chrome 调试端口就绪（最多 15 秒）
    for i in range(15):
        time.sleep(1)
        if is_chrome_debug_running():
            print(f"✅ Chrome 已就绪（等待了 {i+1} 秒）")
            return proc
        print(f"  ⏳ 等待 Chrome 启动... ({i+1}/15)")

    proc.kill()
    raise RuntimeError("❌ Chrome 启动超时，请检查路径是否正确：" + CHROME_PATH)

def main():
    client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)

    # 自动启动 Chrome（如已运行则复用）
    chrome_proc = launch_chrome_with_debug_port()

    print("🚀 正在连接浏览器 (CDP 9222)...")
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp("http://localhost:9222")
            context = browser.contexts[0] if browser.contexts else browser
            page = context.pages[0] if context.pages else context.new_page()
        except Exception as e:
            print(f"❌ 浏览器连接失败: {e}")
            if chrome_proc:
                chrome_proc.kill()
            return

        # 使用官方内置 Computer Use Tool 配置
        generate_content_config = types.GenerateContentConfig(
            temperature=0.0,
            system_instruction="你是一个浏览器自动化助手。请使用提供的工具操作浏览器，完成任务后提供最终结果。",
            tools=[
                types.Tool(
                    computer_use=types.ComputerUse(
                        environment=types.Environment.ENVIRONMENT_BROWSER,
                    )
                ),
            ]
        )

        contents = []
        print("\n🤖 Gemini Computer Use Agent 已就绪 (输入 'exit' 退出)\n")

        while True:
            if len(contents) == 0 or not has_function_calls_in_history(contents):
                user_msg = input("\n👨‍💻 请输入指令: ")
                if user_msg.lower() in ['exit', 'quit']: break
                if not user_msg.strip(): continue
                
                print("📸 捕获屏幕...")
                try:
                    # 使用 10s 超时并禁用动画
                    screenshot_bytes = page.screenshot(timeout=10000, animations="disabled")
                except Exception as e:
                    print(f"  [⚠️ 截图警告] 初始截图失败: {e}")
                    screenshot_bytes = b"" # 允许模型在没有截图的情况下重试或直接通过文本工作
                
                parts = [types.Part.from_text(text=user_msg)]
                if screenshot_bytes:
                    parts.append(types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png"))
                
                contents.append(types.Content(role="user", parts=parts))

            print("🧠 Gemini 思考中...")
            try:
                response = client.models.generate_content(
                    model=MODEL_ID, contents=contents, config=generate_content_config,
                )
            except Exception as e:
                print(f"❌ API 错误: {e}")
                break

            contents.append(response.candidates[0].content)

            # 打印 Gemini 的思考过程 (如果存在 text part)
            for part in response.candidates[0].content.parts:
                if getattr(part, 'text', None):
                    print(f"\n💡 Gemini 想法:\n{part.text.strip()}\n")

            if not has_function_calls(response):
                print(f"\n✅ Gemini:\n{response.text or '[任务完成]'}")
                continue

            # 执行动作并直接生成带有截图的符合 Computer Use 规范的 feedback
            feedback_content = execute_and_create_feedback(response, page)
            contents.append(feedback_content)

            # --- 允许用户插入补充 Comment 的逻辑 ---
            print("\n⏳ [按 ENTER 键中断以提供补充信息... 3秒后自动继续]")
            i, o, e = select.select([sys.stdin], [], [], 3.0) # 3秒无阻塞等待
            if i:
                # 用户按了回车
                sys.stdin.readline() # 消耗掉刚才按的回车
                hint = input("🎤 请输入你的补充/纠正意见 (直接回车跳过): ")
                if hint.strip():
                    # 将用户的 hint 作为新的一条 User 消息追加进去，不用带截图，仅文本
                    print("📝 已记录你的补充意见，将连同最新屏幕一起发送给模型。")
                    # 这里为了不破坏 feedback_content (里面已经有截图了)，我们直接再追加一条纯文本
                    contents.append(
                        types.Content(
                            role="user", 
                            parts=[types.Part.from_text(text=f"用户补充指令: {hint}")]
                        )
                    )
            else:
                pass # 没按回车，自动进入下一轮


if __name__ == "__main__":
    main()
