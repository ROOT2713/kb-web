#!/usr/bin/env python3
"""kb-web 数据分类脚本：按文件名/标题给现有文档分配 bank
不改动 Hindsight 数据，只更新 meta.db"""
import subprocess, json, sqlite3, sys, os

HINDSIGHT_URL = "http://localhost:8888"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meta.db")

def hs_get(path):
    result = subprocess.run(
        ["curl", "-s", "--noproxy", "*", f"{HINDSIGHT_URL}{path}"],
        capture_output=True, text=True, timeout=30
    )
    return json.loads(result.stdout)

def classify(title, filename, text_sample=""):
    combined = f"{title} {filename} {text_sample}".lower()
    
    # Skip test files and MinerU raw
    if "test_" in combined or "mineru_markdown" in combined.lower():
        return "skip"
    if not text_sample.strip() and "mineru" in combined.lower():
        return "skip"
    
    # Proposals (方案/编写规范/立项/咨询/解读/指引)
    for kw in ["方案编写", "立项设计", "咨询", "解读", "指引", "指导书", "编写规范"]:
        if kw in combined:
            return "proposals"
    
    # Assessment (测评/监理/造价/等保/密码/信创)
    for kw in ["测评", "监理", "造价", "等保", "密码应用", "信创", "验收规范", "验收测试"]:
        if kw in combined:
            return "assessment"
    
    # Projects (项目管理/办法/细则/处置)
    for kw in ["项目管理", "管理办法", "验收管理", "细则", "处置办法", "行业基准", "财政投资"]:
        if kw in combined:
            return "projects"
    
    return "projects"

def main():
    print("📦 读取 kb bank 文档列表...")
    data = hs_get("/v1/default/banks/kb/documents?limit=1000")
    items = data.get("items", [])
    
    # Group by doc_id
    docs = {}
    for item in items:
        tags = item.get("tags", [])
        doc_id = None
        for t in tags:
            if t.startswith("doc_id:"):
                doc_id = t[7:]
        if not doc_id:
            continue
        
        if doc_id not in docs:
            title = "?"; fname = "?"
            for t in tags:
                if t.startswith("title:"): title = t[6:]
                if t.startswith("doc:") and not t.startswith("doc_id:"): fname = t[4:]
            docs[doc_id] = {"title": title, "filename": fname, "chunks": 0}
        docs[doc_id]["chunks"] += 1
    
    print(f"   去重后 {len(docs)} 份文档\n")
    
    # Classify and update meta.db
    db = sqlite3.connect(DB_PATH)
    try:
        db.execute("ALTER TABLE doc_meta ADD COLUMN bank TEXT DEFAULT 'kb'")
        print("DB: 已添加 bank 列")
    except sqlite3.OperationalError:
        pass
    
    stats = {"proposals": 0, "assessment": 0, "projects": 0, "skip": 0}
    bank_labels = {"proposals": "📋方案", "assessment": "📊测评", "projects": "📁项目", "skip": "⏭跳过"}
    
    for did, d in sorted(docs.items()):
        bank = classify(d["title"], d["filename"], "")
        stats[bank] += 1
        
        # Update meta.db if this doc_id exists
        db.execute("UPDATE doc_meta SET bank = ? WHERE doc_id = ?", (bank, did))
        
        label = bank_labels[bank]
        print(f"  {label} | {d['title'][:55]:55} | {d['chunks']:3} chunks")
    
    db.commit()
    db.close()
    
    print(f"\n✅ 分类完成")
    for bank in ["proposals", "assessment", "projects", "skip"]:
        print(f"   {bank_labels[bank]}: {stats[bank]} 份")
    print(f"\n数据仍在 kb bank，meta.db 已记录 bank 映射。")

if __name__ == "__main__":
    main()
