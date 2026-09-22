from __future__ import annotations

import argparse
import json
import sys
import urllib.request


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="固定検証実行器へ実行を依頼します")
    p.add_argument("--url", required=True)
    p.add_argument("--token", required=True)
    args = p.parse_args(argv)
    request = urllib.request.Request(
        args.url, method="POST", headers={"Authorization": f"Bearer {args.token}"}, data=b"{}"
    )
    try:
        with urllib.request.urlopen(request, timeout=3600) as response:
            result = json.loads(response.read().decode("utf-8"))
            rendered = json.dumps(result, ensure_ascii=False, indent=2)
            print(rendered, file=sys.stdout if result.get("success") else sys.stderr)
            return 0 if response.status == 200 and result.get("success") is True else 1
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
