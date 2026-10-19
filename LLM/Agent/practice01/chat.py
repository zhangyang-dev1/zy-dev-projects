# DeepSeek 一次性对话 | 无需安装任何依赖（只用标准库）
# 地址和模型写在 config.ini 中:
#   base_url = https://your-api-url
#   model    = deepseek-chat
import sys
import json
import time
import configparser
from pathlib import Path
from urllib import request


def main():
    cfg = configparser.ConfigParser()
    cfg.read(Path(__file__).with_name("config.ini"), encoding="utf-8")
    llm = cfg["llm"]

    messages = []

    while True:
        question = " ".join(sys.argv[1:]) or input("请输入你的问题: ")
        messages.append({"role": "user", "content": question})

        data = json.dumps({
            "model": llm["model"],
            "messages": messages,
        }).encode("utf-8")

        req = request.Request(
            llm["base_url"] + "/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + llm["api_key"],
            },
        )

        try:
            with request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"请求失败: {e}")
            return

        answer = result["choices"][0]["message"]["content"]
        print("DeepSeek:", end=" ", flush=True)
        for char in answer:
            print(char, end="", flush=True)
            time.sleep(0.05)
        print()
        messages.append({"role": "assistant", "content": answer})


if __name__ == "__main__":
    main()
