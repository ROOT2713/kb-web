#!/usr/bin/env python3
"""kb-web 数据迁移脚本：kb bank → proposals/assessment/projects"""
import subprocess, json, sqlite3, time, sys, os

HINDSIGHT_URL = "http://localhost:8888"
KB_BANK = "kb"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meta.db")

BANK_MAP = {
    "proposals": "方案库",
    "assessment": "测评库",
    "projects": "项目库",
}

def hs_get(path):
    result = subprocess.run(
        ["curl", "-s", "--noproxy", "*", f"{HINDSIGHT_URL}{path}"],
        capture_output=True, text=True, timeout=30
    )
    return json.loads(result.stdout)

def hs_post(path, data, timeout=120):
    result = subprocess.run(
        ["curl", "-s", "--noproxy", "*", "-X", "POST", f"{HINDSIGHT_URL}{path}",
         "-H", "Content-Type: application/json", "-d", json.dumps(data)],
        capture_output=True, text=True, timeout=timeout
    )
    return json.loads(result.stdout)

def classify(title, filename, text_sample=""):
    combined = f"{title} {filename} {text_sample}".lower()
    
    # Skip test files and MinerU raw
    if "test_" in combined or "mineru_markdown" in combined.lower():
        return "skip"
    if not text_sample.strip() and "mineru" in combined.lower():
        return "skip"
    
    # Proposals first (方案/编写规范/立项/咨询/解读/指引)
    proposals_kw = ["方案编写", "立项设计", "咨询", "解读", "指引", "指导书", "编写规范"]
    for kw in proposals_kw:
        if kw in combined:
            return "proposals"
    
    # Assessment (测评/监理/造价/等保/密码/信创)
    assessment_kw = ["测评", "监理", "造价", "等保", "密码应用", "信创", "验收规范", "验收测试"]
    for kw in assessment_kw:
        if kw in combined:
            return "assessment"
    
    # Projects (项目管理/办法/细则/处置)
    projects_kw = ["项目管理", "管理办法", "验收管理", "细则", "处置办法", "行业基准", "财政投资"]
    for kw in projects_kw:
        if kw in combined:
            return "projects"
    
    return "projects"

def main():
    # ── 1. Get all chunks from kb bank ──
    print("📦 读取 kb bank 所有文档...")
    data = hs_get(f"/v1/default/banks/{KB_BANK}/documents?limit=1000")
    items = data.get("items", [])
    print(f"   共 {len(items)} 个条目")
    
    # ── 2. Group by doc_id ──
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
            title = "?"; fname = "?"; cat = ""
            for t in tags:
                if t.startswith("title:"): title = t[6:]
                if t.startswith("doc:") and not t.startswith("doc_id:"): fname = t[4:]
                if t.startswith("cat:"): cat = t[4:]
            docs[doc_id] = {
                "title": title, "filename": fname, "category": cat,
                "chunks": [], "text_sample": ""
            }
        docs[doc_id]["chunks"].append({
            "text": item.get("text", ""),
            "tags": item.get("tags", []),
            "id": item.get("id", "")
        })
        if not docs[doc_id]["text_sample"]:
            docs[doc_id]["text_sample"] = (item.get("text", "") or "")[:200]
    
    print(f"   去重后 {len(docs)} 份文档\n")
    
    # ── 3. Classify ──
    plan = {"proposals": [], "assessment": [], "projects": [], "skip": []}
    for did, d in docs.items():
        bank = classify(d["title"], d["filename"], d["text_sample"])
        plan[bank].append(did)
    
    print("=== 分类结果 ===")
    for bank_name in ["proposals", "assessment", "projects", "skip"]:
        count = len(plan[bank_name])
        label = BANK_MAP.get(bank_name, "跳过")
        print(f"  {label}: {count} 份")
        for did in plan[bank_name]:
            d = docs[did]
            print(f"    - {d['title'][:60]} ({len(d['chunks'])} chunks)")
    
    # ── 4. Confirm ──
    print(f"\n⚠ 将把 {len(plan['proposals']) + len(plan['assessment']) + len(plan['projects'])} 份文档迁移到新 bank")
    print("   kb bank 数据不删除，新 bank 是复制。")
    print("   迁移过程中可能比较慢（每份文档需要调用 Hindsight retain API）")
    
    # ── 5. Migrate ──
    db = sqlite3.connect(DB_PATH)
    # Ensure bank column exists
    try:
        db.execute("ALTER TABLE doc_meta ADD COLUMN bank TEXT DEFAULT 'kb'")
        print("DB: 已添加 bank 列")
    except sqlite3.OperationalError:
        pass
    
    total_chunks = 0
    for bank_name in ["proposals", "assessment", "projects"]:
        doc_ids = plan[bank_name]
        if not doc_ids:
            continue
        
        print(f"\n📤 迁移到 {BANK_MAP[bank_name]} ({bank_name})...")
        
        for did in doc_ids:
            d = docs[did]
            chunks_data = d["chunks"]
            
            # Prepare memory items for new bank with bank tag
            memory_items = []
            for i, chunk in enumerate(chunks_data):
                text = chunk["text"].strip()
                if not text:
                    continue
                # Copy original tags + add bank tag
                new_tags = list(chunk["tags"])
                new_tags.append(f"bank:{bank_name}")
                memory_items.append({
                    "content": text,
                    "tags": new_tags,
                    "type": "world"
                })
            
            if not memory_items:
                print(f"  ⏭ {d['title'][:50]} — 无有效内容，跳过")
                continue
            
            # Retain to target bank
            try:
                dyn_timeout = max(60, min(len(memory_items) * 5, 600))
                result = hs_post(
                    f"/v1/default/banks/{bank_name}/memories",
                    {"items": memory_items},
                    timeout=dyn_timeout
                )
                retained = result.get("items_count", 0)
                total_chunks += retained
                print(f"  ✓ {d['title'][:50]} — {retained}/{len(memory_items)} chunks")
            except Exception as e:
                print(f"  ✗ {d['title'][:50]} — 失败: {e}")
                continue
            
            # Update meta.db
            db.execute(
                "UPDATE doc_meta SET bank = ? WHERE doc_id = ?",
                (bank_name, did)
            )
            db.commit()
            
            # Small delay between documents
            time.sleep(1)
    
    db.close()
    print(f"\n✅ 迁移完成！共 {total_chunks} 个 chunks")
    print(f"   proposals: {len(plan['proposals'])} 份")
    print(f"   assessment: {len(plan['assessment'])} 份")
    print(f"   projects: {len(plan['projects'])} 份")
    print(f"   skip: {len(plan['skip'])} 份")
    print(f"\n原始 kb bank 数据未删除，保留作为「全部」回退。")

if __name__ == "__main__":
    main()
