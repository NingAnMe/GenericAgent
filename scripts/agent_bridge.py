#!/usr/bin/env python3
"""stdin/stdout JSON-RPC bridge between Gateway and GenericAgent."""
import os, sys, json, threading, argparse, signal, hashlib

GA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, GA_DIR)

from llmcore import reload_mykeys, resolve_client, NativeToolClient, ToolClient
from agent_loop import agent_runner_loop, exhaust
from ga import GenericAgentHandler, smart_format, get_global_memory

class BridgeHandler(GenericAgentHandler):
    def __init__(self, history, temp_dir):
        super().__init__(type('Agent', (), {
            'task_dir': os.path.join(temp_dir, 'task'),
            'history': history,
            'lock': threading.Lock(),
            'stop_sig': False,
            'is_running': True,
        })(), history, os.path.join(temp_dir, 'task'))

def load_client():
    mykeys, _ = reload_mykeys()
    for k, cfg in mykeys.items():
        if any(x in k for x in ['api', 'config', 'cookie']):
            try:
                c = resolve_client(k)
                if c: return c
            except: pass
    raise RuntimeError('No valid LLM config found in mykey.py')

def load_tools():
    p = os.path.join(GA_DIR, 'assets', 'tools_schema.json')
    with open(p, 'r', encoding='utf-8') as f:
        schema = f.read()
    if os.name != 'nt':
        schema = schema.replace('powershell', 'bash')
    return json.loads(schema)

def get_sys_prompt(user_dir):
    lang = os.environ.get('GA_LANG', 'zh')
    suffix = '_en' if lang == 'en' else ''
    p = os.path.join(GA_DIR, f'assets/sys_prompt{suffix}.txt')
    with open(p, 'r', encoding='utf-8') as f:
        prompt = f.read()
    import time
    prompt += f"\nToday: {time.strftime('%Y-%m-%d %a')}\n"
    prompt += get_global_memory()
    prompt += f"\n\n[System] You are an AI Copilot helping a user manage datasets."
    prompt += f" You have access to files under {user_dir}/"
    return prompt

def format_context(text):
    """将前端 JSON 上下文转为人类可读文本"""
    if not text:
        return ""
    try:
        ctx = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text

    page_labels = {
        'datasets': '数据集列表',
        'dataset-detail': '数据集详情',
        'agents': '智能体',
        'evaluations': '评测',
        'settings': '设置',
    }
    lines = [f"当前页面：{page_labels.get(ctx.get('page', ''), ctx.get('page', ''))}"]

    if ctx.get('datasetName'):
        lines.append(f"数据集：{ctx['datasetName']} ({ctx.get('datasetId', '')})")

    checklist = ctx.get('checklist', [])
    if checklist:
        lines.append('待完成事项：')
        for item in checklist:
            lines.append(f'- {item}')

    return '\n'.join(lines)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--user-dir', required=True)
    args = parser.parse_args()

    user_dir = os.path.abspath(args.user_dir)
    os.makedirs(user_dir, exist_ok=True)
    temp_dir = os.path.join(user_dir, 'ga_temp')
    os.makedirs(temp_dir, exist_ok=True)

    client = load_client()
    tools = load_tools()
    history = []
    last_context_hash = None
    current_context_text = ""

    def emit(msg):
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + '\n')
        sys.stdout.flush()

    def run_task(content):
        nonlocal history, last_context_hash, current_context_text
        sys_prompt = get_sys_prompt(user_dir)

        # Hash 去重：只在上下文变化后的第一条消息注入前缀
        ctx_text = format_context(current_context_text)
        ctx_hash = hashlib.sha256(ctx_text.encode()).hexdigest() if ctx_text else None
        if ctx_hash and ctx_hash != last_context_hash:
            content = f"[页面上下文]\n{ctx_text}\n\n[用户消息]\n{content}"
            last_context_hash = ctx_hash

        handler = BridgeHandler(history, temp_dir)
        handler.verbose = False

        gen = agent_runner_loop(
            client, sys_prompt, content, handler, tools,
            max_turns=70, verbose=False
        )

        try:
            for chunk in gen:
                if not chunk: continue
                if chunk.startswith('\n\n**Turn'):
                    emit({'type': 'token', 'content': '\n'})
                    continue
                emit({'type': 'token', 'content': chunk})
        except Exception as e:
            emit({'type': 'error', 'message': str(e)})
        finally:
            emit({'type': 'done'})

    run_task("Hello — initialization complete. I'm ready to assist with dataset management.")

    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        if msg.get('type') == 'chat':
            run_task(msg['content'])
        elif msg.get('type') == 'abort':
            emit({'type': 'done'})
        elif msg.get('type') == 'context':
            current_context_text = msg.get('text', '')

if __name__ == '__main__':
    main()
