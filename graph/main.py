from __future__ import annotations

import sys

if __package__:
    from .model import load_graph
else:
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from graph.model import load_graph

USAGE = "用法：python -m graph.main [check | list | company <ticker> | theme <名稱>]"


def main(argv: list[str]) -> int:
    g = load_graph()
    cmd = argv[0] if argv else "check"

    if cmd == "check":
        issues, external = g.check()
        if not issues:
            print(f"✓ 一致性檢查通過（{len(g.companies)} 家公司、{len(g.themes)} 個主題）")
        else:
            print(f"發現 {len(issues)} 個問題（主題引用未定義公司）：")
            for i in issues:
                print("  -", i)
        if external:
            print(f"（供參考）關係邊指向的圖外實體 {len(external)} 個：", "、".join(external))
        return 0

    if cmd == "list":
        for t in g.themes:
            subs = "、".join(st["name"] for st in t.get("subthemes", []))
            print(f"● {t['name']}：{subs}")
        print("\n公司：", "、".join(g.companies))
        return 0

    if cmd == "company" and len(argv) > 1:
        tk = argv[1].upper()
        c = g.company(tk)
        if not c:
            print(f"找不到 {tk}")
            return 1
        n = g.neighbors(tk)
        print(f"# {tk} — {c.get('name','')}（{c.get('role','')}）")
        print("  主題：", "、".join(g.themes_of(tk)) or "—")
        print("  上游（供應商）：", "、".join(n["upstream"]) or "—")
        print("  下游（客戶）：", "、".join(n["downstream"]) or "—")
        print("  競爭對手：", "、".join(n["competitors"]) or "—")
        return 0

    if cmd == "theme" and len(argv) > 1:
        name = argv[1]
        comps = g.companies_in(name)
        print(f"# 主題/子題「{name}」相關公司：", "、".join(comps) or "（無，請確認名稱）")
        return 0

    if cmd == "suggest" and len(argv) > 1:
        from .suggest import suggest, to_yaml_block
        from reports.llm import get_api_key

        tk = argv[1].upper()
        c = g.company(tk) or {}
        draft, cost = suggest(tk, g, get_api_key())
        print(f"# {tk} 供應鏈關係草稿（Opus 4.8，請審核）　成本 ${cost:.4f}\n")
        print("角色：", draft.get("role", ""))
        print("上游（供應商）：", "、".join(map(str, draft.get("upstream") or [])))
        print("下游（客戶）：", "、".join(map(str, draft.get("downstream") or [])))
        print("競爭對手：", "、".join(map(str, draft.get("competitors") or [])))
        print("相關主題：", "、".join(map(str, draft.get("themes") or [])))
        print("\n審核提醒：")
        for n in draft.get("notes") or []:
            print("  -", n)
        print("\n可貼進 graph.yaml 的片段（審核後）：\n")
        print(to_yaml_block(tk, c.get("name", draft.get("name", tk)), draft))
        return 0

    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
