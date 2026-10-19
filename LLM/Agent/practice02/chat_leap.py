# DeepSeek function call 示例: getDate + isLeapYear 两个工具 | 无需安装任何依赖（只用标准库）
# getDate  通过本地 date 命令获取电脑当前日期和时间
# isLeapYear 通过 python 代码计算年份是否闰年
# 地址和模型写在 config.ini 中:
#   base_url = https://your-api-url
#   model    = deepseek-chat
import sys
import json
import time
import subprocess
import configparser
from pathlib import Path
from urllib import request

# 告诉 LLM 有哪些工具可用
TOOL_GET_DATE = {
    "type": "function",
    "function": {
        "name": "getDate",
        "description": "获取当前电脑的日期和时间，通过本地 date 命令实现。当用户询问现在几点、今天几号等时间日期问题时调用",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

TOOL_IS_LEAP_YEAR = {
    "type": "function",
    "function": {
        "name": "isLeapYear",
        "description": "判断一个年份是否闰年。当用户询问某年是否闰年时调用",
        "parameters": {
            "type": "object",
            "properties": {
                "year": {
                    "type": "integer",
                    "description": "要判断的年份，例如 2024",
                },
            },
            "required": ["year"],
        },
    },
}


def run_date():
    # 本地 date 命令取日期，time 命令取时间（cmd 内置命令）
    out = subprocess.run("date /t & time /t", shell=True, capture_output=True)
    raw = out.stdout or out.stderr
    # date 命令输出编码随控制台环境变化，先按 utf-8 解，失败再按 gbk 解
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp936", errors="replace")
    return text.strip()


def is_leap_year(year):
    # 普通年: 能被4整除且不能被100整除; 世纪年: 能被400整除
    return (year % 4 == 0 and year % 100 != 0) or year % 400 == 0


def send(llm, messages):
    data = json.dumps({
        "model": llm["model"],
        "messages": messages,
        "tools": [TOOL_GET_DATE, TOOL_IS_LEAP_YEAR],
    }).encode("utf-8")

    req = request.Request(
        llm["base_url"] + "/chat/completions",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + llm["api_key"],
        },
    )
    with request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    cfg = configparser.ConfigParser()
    cfg.read(Path(__file__).with_name("config.ini"), encoding="utf-8")
    llm = cfg["llm"]

    messages = []

    while True:
        question = " ".join(sys.argv[1:]) or input("请输入你的问题: ")
        messages.append({"role": "user", "content": question})

        # 反复请求，直到 LLM 不再调用工具、给出最终回答
        while True:
            try:
                result = send(llm, messages)
            except Exception as e:
                print(f"请求失败: {e}")
                return

            msg = result["choices"][0]["message"]
            messages.append(msg)

            if not msg.get("tool_calls"):
                break

            for tc in msg["tool_calls"]:
                name = tc["function"]["name"]
                args = json.loads(tc["function"].get("arguments") or "{}")
                if name == "getDate":
                    content = run_date()
                elif name == "isLeapYear":
                    year = int(args["year"])
                    content = f"{year}年{'是闰年' if is_leap_year(year) else '不是闰年'}"
                else:
                    content = "未知工具"
                print(f"[调用工具 {name}] {content}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": content,
                })

        answer = msg["content"]
        print("DeepSeek:", end=" ", flush=True)
        for char in answer:
            print(char, end="", flush=True)
            time.sleep(0.05)
        print()


if __name__ == "__main__":
    main()
