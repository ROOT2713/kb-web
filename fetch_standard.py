#!/usr/bin/env python3
"""
标准下载脚本 — 从公开来源下载国家标准并入库到 kb-web

用法:
  python3 fetch_standard.py "GB 50348"                    # 单个标准
  python3 fetch_standard.py "GB 50348" "GB/T 22239"       # 批量
  python3 fetch_standard.py --bank assessment "GB 50348"  # 指定入库 bank

依赖: requests, anysearch CLI（已装）
"""
import sys, os, json, time, subprocess, hashlib, uuid
from pathlib import Path
import requests

HINDSIGHT_URL = "http://localhost:8888"
ANYSEARCH_CLI = os.path.expanduser("~/.agents/skills/anysearch/scripts/anysearch_cli.py")

def search_standard(std_no):
    """用 AnySearch 搜索标准 PDF"""
    result = subprocess.run(
        ["python3", ANYSEARCH_CLI, "search", f"{std_no} 标准 PDF下载", "-m", "5", "--freshness", "year"],
        capture_output=True, text=True, timeout=30
    )
    # Parse markdown output for URLs
    urls = []
    for line in result.stdout.split('\n'):
        if '**URL**: ' in line:
            urls.append(line.split('**URL**: ')[1].strip())
    return urls

def download_pdf(url, std_no):
    """下载 PDF"""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    r = requests.get(url, headers=headers, timeout=60, allow_redirects=True)
    if r.status_code == 200 and 'application/pdf' in r.headers.get('Content-Type', ''):
        filename = f"/tmp/{std_no.replace('/', '_').replace(' ', '_')}.pdf"
        with open(filename, 'wb') as f:
            f.write(r.content)
        return filename, len(r.content)
    return None, 0

def parse_pdf(filepath):
    """快速解析 PDF 文本（用 pypdf）"""
    import pypdf
    text = []
    with open(filepath, 'rb') as f:
        reader = pypdf.PdfReader(f)
        for page in reader.pages:
            t = page.extract_text()
            if t:
                text.append(t)
    return '\n'.join(text)

def upload_to_hindsight(text, title, bank="kb"):
    """上传到 Hindsight + SQLite"""
    chunk_size = 1000
    chunks = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]
    doc_id = str(uuid.uuid4())
    
    session = requests.Session()
    success = 0
    for i, chunk in enumerate(chunks):
        if not chunk.strip():
            continue
        payload = [{
            "text": chunk.strip(),
            "tags": [
                f"doc:{title}.pdf",
                f"chunk:{i+1}/{len(chunks)}",
                f"doc_id:{doc_id}",
                f"title:{title}",
            ]
        }]
        r = session.post(
            f"{HINDSIGHT_URL}/v1/default/banks/{bank}/memories",
            json=payload, timeout=30
        )
        if r.status_code in (200, 201):
            success += 1
    
    # Write to meta.db
    import sqlite3
    meta_db = os.path.expanduser("~/kb-web/meta.db")
    db = sqlite3.connect(meta_db)
    db.execute(
        "INSERT OR REPLACE INTO doc_meta (doc_id, title, category, filename, content_hash, bank, created_at) VALUES (?, ?, '', ?, '', ?, datetime('now'))",
        (doc_id, title, f"{title}.pdf", bank)
    )
    db.commit()
    db.close()
    
    return doc_id, success, len(chunks)

def main():
    import argparse
    parser = argparse.ArgumentParser(description="下载国家标准并入库")
    parser.add_argument("standards", nargs="+", help="标准号，如 'GB 50348'")
    parser.add_argument("--bank", default="kb", help="入库 bank (默认 kb)")
    args = parser.parse_args()
    
    for std_no in args.standards:
        print(f"\n{'='*50}")
        print(f"🔍 搜索: {std_no}")
        urls = search_standard(std_no)
        if not urls:
            print(f"❌ 未找到 {std_no} 的下载链接")
            continue
        
        pdf_path = None
        for url in urls[:3]:
            print(f"  📥 尝试: {url[:80]}...")
            pdf_path, size = download_pdf(url, std_no)
            if pdf_path:
                print(f"  ✅ 下载成功 ({size/1024/1024:.1f}MB)")
                break
        
        if not pdf_path:
            print(f"❌ 下载失败")
            continue
        
        print(f"  📖 解析中...")
        text = parse_pdf(pdf_path)
        print(f"  📝 提取 {len(text)} 字符")
        
        print(f"  💾 入库中...")
        doc_id, ok_chunks, total = upload_to_hindsight(text, f"{std_no}", args.bank)
        print(f"  ✅ 入库完成: {ok_chunks}/{total} chunks, doc_id={doc_id[:8]}...")
        
        # Clean up
        os.remove(pdf_path)

if __name__ == "__main__":
    main()
