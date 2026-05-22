#!/usr/bin/env python3
"""
知识库网页服务 — 基于 Hindsight + DeepSeek
文档上传 → 自动切片入库 → 自然语言搜索 → AI 合成答案
v2: 增加标题/分类系统，本地 SQLite 存储文档元数据
"""
import os, sys, json, re, uuid, sqlite3, subprocess, tempfile, shutil, traceback, hashlib
from pathlib import Path
from datetime import datetime
from io import BytesIO
import zipfile as _zipfile
import time as _time

import httpx
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
import pypdf
import docx

# ─── 环境 ────────────────────────────────────────────────────
ENV_FILE = os.path.expanduser("~/.hermes/.env")
if os.path.exists(ENV_FILE):
    for line in open(ENV_FILE):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"
HINDSIGHT_URL = "http://localhost:8888"
KB_BANK = "kb"  # 默认 bank（兼容旧数据，"全部"查询时用）

# ── 多 Bank 配置 ──
BANKS = {
    "all":        {"name": "全部",     "hindsight": "kb", "prompt": "通用政务信息化知识库"},
    "tech":       {"name": "技术实践", "hindsight": "kb", "prompt": "你是软件开发技术专家。精通前端/后端/Agent/DevOps，回答注重实战经验和架构设计。"},
    "security":   {"name": "安全研究", "hindsight": "kb", "prompt": "你是网络安全研究专家。精通渗透测试/漏洞分析/防御技术，回答注重技术细节和攻防思路。"},
    "ai":         {"name": "AI探索",   "hindsight": "kb", "prompt": "你是AI技术专家。精通LLM/机器学习/模型训练，回答注重技术原理和实践经验。"},
    "notes":      {"name": "综合笔记", "hindsight": "kb", "prompt": "你是知识管理助手。擅长整理归纳各类知识，回答清晰有条理。"},
    "proposals":  {"name": "方案库",   "hindsight": "kb", "prompt": "你是政务信息化项目方案专家。擅长解读政策文件、编写项目方案、提供立项咨询建议。回答时注重政策依据、方案结构和可行性分析。"},
    "assessment": {"name": "测评库",   "hindsight": "kb", "prompt": "你是政务信息化验收测评专家。精通等保测评、密码应用测评、软件造价评估、监理服务规范。回答时注重标准条款、测评要点和合规要求。"},
    "projects":   {"name": "项目库",   "hindsight": "kb", "prompt": "你是政务信息化项目管理专家。熟悉项目管理办法、验收管理细则、财政投资规定。回答时注重管理流程、审批要求和实操经验。"},
}

# MinerU API 配置
MINERU_TOKEN = os.environ.get("MINERU_API_TOKEN", "")
MINERU_BASE = "https://mineru.net/api/v4"
MINERU_PAGES_MAX = 200  # 单次API调用的最大页数

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "meta.db")

app = FastAPI(title="知识库", docs_url=None)

# ─── 默认分类列表 ────────────────────────────────────────────────
DEFAULT_CATEGORIES = [
    "💡想法", "💼工作", "📚学习", "🏠生活", "🚀项目",
    "💭灵感", "📝会议", "🔧技术", "📊数据", "📰资讯",
    "🔒安全", "🤖AI", "其他",
]

# ─── SQLite 元数据 ──────────────────────────────────────────────
def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db

def init_db():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS doc_meta (
            doc_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            category TEXT DEFAULT '',
            filename TEXT DEFAULT '',
            content_hash TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    # 兼容旧表：补加 content_hash 列
    try:
        db.execute("ALTER TABLE doc_meta ADD COLUMN content_hash TEXT DEFAULT ''")
        print("DB: 已添加 content_hash 列", file=sys.stderr)
    except sqlite3.OperationalError:
        pass  # 列已存在
    db.commit()
    db.close()

init_db()

def save_meta(doc_id: str, title: str, category: str, filename: str, content_hash: str = ""):
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO doc_meta (doc_id, title, category, filename, content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (doc_id, title, category, filename, content_hash, datetime.utcnow().isoformat()),
    )
    db.commit()
    db.close()

def get_meta(doc_id: str) -> dict:
    db = get_db()
    row = db.execute("SELECT * FROM doc_meta WHERE doc_id = ?", (doc_id,)).fetchone()
    db.close()
    if row:
        return {"title": row["title"], "category": row["category"], "filename": row["filename"], "created_at": row["created_at"]}
    return {"title": "未知文档", "category": "", "filename": "未知", "created_at": ""}

def get_all_meta() -> dict:
    """返回 {doc_id: {...}}"""
    db = get_db()
    rows = db.execute("SELECT * FROM doc_meta ORDER BY created_at DESC").fetchall()
    db.close()
    result = {}
    for r in rows:
        result[r["doc_id"]] = {"title": r["title"], "category": r["category"], "filename": r["filename"], "created_at": r["created_at"]}
    return result

def update_meta(doc_id: str, title: str = None, category: str = None):
    db = get_db()
    if title is not None and category is not None:
        db.execute("UPDATE doc_meta SET title=?, category=? WHERE doc_id=?", (title, category, doc_id))
    elif title is not None:
        db.execute("UPDATE doc_meta SET title=? WHERE doc_id=?", (title, doc_id))
    elif category is not None:
        db.execute("UPDATE doc_meta SET category=? WHERE doc_id=?", (category, doc_id))
    db.commit()
    db.close()

def delete_meta(doc_id: str):
    db = get_db()
    db.execute("DELETE FROM doc_meta WHERE doc_id = ?", (doc_id,))
    db.commit()
    db.close()

def find_by_hash(content_hash: str) -> dict | None:
    """按内容哈希查找已存在的文档，返回元数据或 None"""
    if not content_hash:
        return None
    db = get_db()
    row = db.execute(
        "SELECT doc_id, title, category, filename, created_at FROM doc_meta WHERE content_hash = ? AND content_hash != ''",
        (content_hash,)
    ).fetchone()
    db.close()
    if row:
        return {
            "doc_id": row["doc_id"],
            "title": row["title"],
            "category": row["category"],
            "filename": row["filename"],
            "created_at": row["created_at"],
        }
    return None

# ─── 工具函数 ──────────────────────────────────────────────────

MAX_FILE_SIZE = 200 * 1024 * 1024  # 200MB（与 MinerU 对齐）

def ocr_pdf(pdf_bytes: bytes) -> str:
    """用 pdftoppm + tesseract 对扫描件 PDF 做 OCR，返回纯文本"""
    tmpdir = tempfile.mkdtemp(prefix="kb_ocr_")
    try:
        pdf_path = os.path.join(tmpdir, "input.pdf")
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)

        # 先探测页数
        page_count = len(pypdf.PdfReader(BytesIO(pdf_bytes)).pages)
        print(f"OCR: {page_count} 页扫描件，开始转换...", file=sys.stderr)

        # PDF → 灰度 PNG（200 DPI 平衡速度与精度）
        try:
            result = subprocess.run(
                ["pdftoppm", "-png", "-gray", "-r", "200", pdf_path, os.path.join(tmpdir, "page")],
                capture_output=True, text=True, timeout=600,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"PDF 转换图片超时（{page_count} 页扫描件处理超过 5 分钟）。"
                "建议先用 PC 端工具将 PDF 压缩/降低分辨率后再上传。"
            )
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            raise RuntimeError(f"PDF 转换异常: {e}")

        if result.returncode != 0:
            raise RuntimeError(f"pdftoppm 失败: {result.stderr[:200]}")

        # 逐页 OCR
        pages = sorted(Path(tmpdir).glob("page-*.png"))
        if not pages:
            raise RuntimeError("PDF 转换后无图片输出")

        texts = []
        for idx, png in enumerate(pages):
            if idx % 5 == 0 and idx > 0:
                print(f"OCR: {idx}/{len(pages)} 页已完成...", file=sys.stderr)
            out_base = os.path.join(tmpdir, f"ocr_{png.stem}")
            try:
                ocr_result = subprocess.run(
                    ["tesseract", str(png), out_base, "-l", "chi_sim+eng", "--psm", "3"],
                    capture_output=True, text=True, timeout=60,
                )
            except subprocess.TimeoutExpired:
                print(f"OCR 页面 {png.name} 超时，跳过", file=sys.stderr)
                continue
            except Exception as e:
                print(f"OCR 页面 {png.name} 异常: {e}", file=sys.stderr)
                continue
            if ocr_result.returncode != 0:
                print(f"OCR 页面 {png.name} 失败: {ocr_result.stderr[:100]}", file=sys.stderr)
                continue
            out_txt = out_base + ".txt"
            if os.path.exists(out_txt):
                with open(out_txt, "r", encoding="utf-8") as f:
                    texts.append(f.read().strip())
            os.unlink(out_txt)

        return "\n\n".join(texts)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

async def mineru_parse_pdf(filename: str, content: bytes) -> str:
    """通过 MinerU API 解析 PDF，返回 Markdown（含HTML表格）。
    
    流程：获取上传URL → PUT文件 → 轮询结果 → 下载ZIP → 提取 full.md
    
    超过 MINERU_PAGES_MAX 页自动分批，合并结果。
    """
    if not MINERU_TOKEN:
        raise RuntimeError("MINERU_API_TOKEN 未配置")
    
    reader = pypdf.PdfReader(BytesIO(content))
    total_pages = len(reader.pages)
    print(f"MinerU: {total_pages} 页，开始解析...", file=sys.stderr)
    
    # 计算分批范围
    ranges = []
    for start in range(0, total_pages, MINERU_PAGES_MAX):
        if start >= total_pages:
            break
        end = min(start + MINERU_PAGES_MAX, total_pages)
        ranges.append((start + 1, end))  # page_ranges 是 1-indexed
    
    print(f"MinerU: 分 {len(ranges)} 批: {ranges}", file=sys.stderr)
    
    all_md_parts = []
    
    for batch_idx, (pg_start, pg_end) in enumerate(ranges):
        batch_label = f"batch{batch_idx+1}"
        page_range = f"{pg_start}-{pg_end}"
        
        # Step 1: 获取上传 URL
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{MINERU_BASE}/file-urls/batch",
                headers={
                    "Authorization": f"Bearer {MINERU_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={
                    "files": [{
                        "name": f"{batch_label}.pdf",
                        "is_ocr": True,
                        "page_ranges": page_range,
                    }],
                    "model_version": "pipeline",
                    "language": "ch",
                    "enable_table": True,
                },
            )
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"MinerU 获取上传URL失败: {data.get('msg')}")
            
            batch_id = data["data"]["batch_id"]
            file_url = data["data"]["file_urls"][0]
        
        # Step 2: 上传文件
        # 如果文件超过200页需要截取，否则传整个文件
        if len(ranges) == 1:
            upload_bytes = content
        else:
            # 截取指定页码范围
            writer = pypdf.PdfWriter()
            for pg in range(pg_start - 1, pg_end):
                writer.add_page(reader.pages[pg])
            upload_buf = BytesIO()
            writer.write(upload_buf)
            upload_bytes = upload_buf.getvalue()
        
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.put(file_url, content=upload_bytes)
            if resp.status_code != 200:
                raise RuntimeError(f"MinerU 文件上传失败: HTTP {resp.status_code}")
        
        print(f"MinerU {batch_label}: 已上传 {pg_start}-{pg_end} 页", file=sys.stderr)
        
        # Step 3: 轮询结果
        poll_url = f"{MINERU_BASE}/extract-results/batch/{batch_id}"
        max_wait = 600  # 最多等10分钟
        
        for _ in range(max_wait // 3):
            await _asleep(3)
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    poll_url,
                    headers={"Authorization": f"Bearer {MINERU_TOKEN}"},
                )
                data = resp.json()
            
            if data.get("code") != 0:
                continue
            
            er = (data.get("data", {}).get("extract_result") or [{}])[0]
            state = er.get("state")
            
            if state == "done":
                zip_url = er.get("full_zip_url")
                break
            elif state == "failed":
                raise RuntimeError(f"MinerU {batch_label} 失败: {er.get('err_msg')}")
        else:
            raise RuntimeError(f"MinerU {batch_label}: 轮询超时 ({max_wait}s)")
        
        # Step 4: 下载并解压 Markdown
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(zip_url)
            zip_data = resp.content
        
        with _zipfile.ZipFile(BytesIO(zip_data)) as zf:
            if "full.md" not in zf.namelist():
                raise RuntimeError(f"MinerU {batch_label}: ZIP 中缺少 full.md")
            md_text = zf.read("full.md").decode("utf-8")
        
        all_md_parts.append(md_text)
        print(f"MinerU {batch_label}: {len(md_text)} 字符", file=sys.stderr)
    
    result = "\n\n".join(all_md_parts)
    print(f"MinerU 完成: {len(result)} 字符", file=sys.stderr)
    return result


async def _asleep(seconds: float):
    """异步 sleep，兼容 Python 3.11-"""
    import asyncio
    await asyncio.sleep(seconds)

async def parse_document(filename: str, content: bytes) -> str:
    """解析 PDF/Word/Markdown/TXT → 纯文本
    
    PDF 优先使用 MinerU API（高精度表格识别），失败时回退到 tesseract OCR。
    """
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        try:
            reader = pypdf.PdfReader(BytesIO(content))
        except Exception as e:
            raise ValueError(f"PDF 解析失败（文件可能已损坏或加密）: {e}")
        if reader.is_encrypted:
            raise ValueError("PDF 文件已加密，无法提取文字内容")
        
        text = "\n\n".join(p.extract_text() or "" for p in reader.pages)
        if not text.strip():
            page_count = len(reader.pages)
            print(f"PDF 文字层为空 ({page_count} 页)，启动 OCR...", file=sys.stderr)
            
            # 优先尝试 MinerU API
            if MINERU_TOKEN:
                try:
                    text = await mineru_parse_pdf(filename, content)
                    if text.strip():
                        print(f"MinerU 完成，提取 {len(text)} 字符", file=sys.stderr)
                        return text
                except Exception as e:
                    print(f"MinerU 失败，回退 tesseract: {e}", file=sys.stderr)
                    traceback.print_exc(file=sys.stderr)
            
            # 回退到 tesseract
            print(f"使用 tesseract OCR ({page_count} 页)...", file=sys.stderr)
            try:
                text = ocr_pdf(content)
                if not text.strip():
                    raise ValueError(
                        "PDF OCR 识别结果为空。"
                        "可能原因：①图片质量过低 ②PDF 为纯图片且文字不清晰。"
                    )
                print(f"tesseract 完成，提取 {len(text)} 字符", file=sys.stderr)
            except RuntimeError as e:
                raise ValueError(f"PDF OCR 失败: {e}")
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                raise ValueError(f"PDF OCR 异常: {e}")
        return text
    elif ext in (".docx", ".doc"):
        try:
            d = docx.Document(BytesIO(content))
        except Exception as e:
            raise ValueError(f"Word 文档解析失败: {e}")
        return "\n\n".join(p.text for p in d.paragraphs)
    elif ext in (".md", ".markdown", ".txt", ".text", ""):
        return content.decode("utf-8", errors="replace")
    else:
        raise ValueError(f"不支持的文件格式: {ext}")

def filename_to_title(filename: str) -> str:
    """从文件名生成默认标题：去掉扩展名"""
    return Path(filename).stem or filename

async def deepseek_chat(messages: list, stream: bool = False) -> str:
    """调用 DeepSeek Chat API"""
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{DEEPSEEK_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"},
            json={
                "model": DEEPSEEK_MODEL,
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": 3000,
                "stream": stream,
            },
            timeout=120,
        )
        if stream:
            return resp
        data = resp.json()
        return data["choices"][0]["message"]["content"]

async def hindsight_request(endpoint: str, method: str = "GET", json_data: dict = None, timeout: int = 30) -> dict:
    """调用 Hindsight API"""
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            if method == "POST":
                resp = await client.post(f"{HINDSIGHT_URL}{endpoint}", json=json_data)
            elif method == "DELETE":
                resp = await client.delete(f"{HINDSIGHT_URL}{endpoint}")
            else:
                resp = await client.get(f"{HINDSIGHT_URL}{endpoint}")
        except httpx.TimeoutException:
            raise Exception(f"Hindsight {method} {endpoint}: 请求超时（{timeout}s）")
        except httpx.ConnectError:
            raise Exception(f"Hindsight {method} {endpoint}: 无法连接（服务未启动？）")
        except Exception as e:
            raise Exception(f"Hindsight {method} {endpoint}: 网络异常: {e}")

        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("detail", "")
            except Exception:
                pass
            detail = detail or resp.text[:200] or f"HTTP {resp.status_code}"
            raise Exception(f"Hindsight {method} {endpoint} returned {resp.status_code}: {detail}")
        try:
            return resp.json()
        except Exception:
            raise Exception(f"Hindsight {method} {endpoint}: 响应不是有效 JSON: {resp.text[:200]}")

async def recall(query: str, limit: int = 5, bank: str = "kb") -> list:
    """语义召回 — 支持指定 bank"""
    result = await hindsight_request(
        f"/v1/default/banks/{bank}/memories/recall",
        "POST",
        {"query": query, "max_tokens": 4096},
    )
    return result.get("results", [])

def get_bank_config(bank_key: str) -> dict:
    """获取 bank 配置，不存在则返回默认"""
    return BANKS.get(bank_key, BANKS["all"])

# ─── API ──────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload(
    file: UploadFile = File(...),
    title: str = Form(""),
    category: str = Form(""),
    bank: str = Form("kb"),
):
    """上传文档 → 解析 → 切片 → 存入 Hindsight（支持指定 bank）"""
    if not file.filename:
        raise HTTPException(400, "文件名不能为空")

    # 验证 bank
    bank_cfg = get_bank_config(bank)
    if bank == "all":
        bank = "kb"  # "全部"默认回到 kb
    hs_bank = bank_cfg["hindsight"]

    content = await file.read()
    if len(content) == 0:
        raise HTTPException(400, "文件为空")

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(400, f"文件过大（{len(content)//1024//1024}MB），上限 {MAX_FILE_SIZE//1024//1024}MB")

    try:
        text = await parse_document(file.filename, content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        raise HTTPException(500, f"文档解析异常: {e}")

    if not text or len(text.strip()) < 10:
        raise HTTPException(400, "文档内容过短")

    # ── 去重检测：按文件内容 SHA256 查重 ──
    content_hash = hashlib.sha256(content).hexdigest()
    existing = find_by_hash(content_hash)
    if existing:
        raise HTTPException(
            409,
            f"文档已存在（内容完全一致）。\n"
            f"已有文档：{existing['title']}（{existing['filename']}）\n"
            f"上传时间：{existing['created_at']}\n"
            f"文档ID：{existing['doc_id']}"
        )

    # 标题：用户指定 > 文件名去扩展名
    doc_title = title.strip() or filename_to_title(file.filename)
    doc_category = category.strip()

    # 分块存入 Hindsight（批量一次提交）
    chunk_size = 1000
    chunks = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]
    doc_id = str(uuid.uuid4())

    memory_items = []
    for i, chunk in enumerate(chunks):
        chunk = chunk.strip()
        if not chunk:
            continue
        tags = [
            f"doc:{file.filename}",
            f"chunk:{i+1}/{len(chunks)}",
            f"doc_id:{doc_id}",
            f"title:{doc_title}",
            f"bank:{bank}",
        ]
        if doc_category:
            tags.append(f"cat:{doc_category}")
        memory_items.append({"content": chunk, "tags": tags, "type": "world"})

    retained = 0
    hindsight_error = None
    if memory_items:
        # 超时按 chunk 数量动态计算：每 chunk 5s，最少 120s，最多 600s
        # 208 chunks 实测需要 ~408s，5s/chunk 给 2.5× 余量
        dyn_timeout = max(120, min(len(memory_items) * 5, 600))
        print(f"Upload: {len(memory_items)} chunks → bank={hs_bank}, timeout={dyn_timeout}s", file=sys.stderr)
        try:
            result = await hindsight_request(
                f"/v1/default/banks/{hs_bank}/memories",
                "POST",
                {"items": memory_items},
                timeout=dyn_timeout,
            )
            retained = result.get("items_count", len(memory_items))
            print(f"Hindsight stored {retained}/{len(memory_items)} chunks for doc {doc_id}", file=sys.stderr)
        except Exception as e:
            hindsight_error = str(e) or repr(e)
            print(f"Hindsight write FAILED for doc {doc_id} ({file.filename}): {hindsight_error}", file=sys.stderr)

    # 如果 Hindsight 完全失败，不存元数据，返回错误
    if hindsight_error and retained == 0:
        raise HTTPException(
            502,
            f"知识库存储服务暂时不可用，请稍后重试。"
            f"（已将 {len(memory_items)} 个文本块上传到向量库但全部失败）"
        )

    # 部分成功：存元数据，返回警告
    if retained < len(memory_items):
        print(f"WARNING: Only {retained}/{len(memory_items)} chunks stored for doc {doc_id}", file=sys.stderr)

    # 保存元数据到 SQLite（含 bank 字段）
    save_meta(doc_id, doc_title, doc_category, file.filename, content_hash)
    # 同时记录 bank 到 meta.db
    db = get_db()
    db.execute("UPDATE doc_meta SET bank = ? WHERE doc_id = ?", (bank, doc_id))
    db.commit()
    db.close()

    return {
        "ok": True,
        "doc_id": doc_id,
        "title": doc_title,
        "category": doc_category,
        "filename": file.filename,
        "chunks": retained,
        "total_chars": len(text),
        "preview": text[:200] + ("..." if len(text) > 200 else ""),
        "warning": f"仅成功入库 {retained}/{len(memory_items)} 个文本片段" if retained < len(memory_items) else None,
    }


@app.post("/api/query")
async def query(q: str = Form(...), bank: str = Form("all")):
    """搜索知识库 → 召回 → DeepSeek 合成答案（支持多 bank）"""
    if not q.strip():
        raise HTTPException(400, "问题不能为空")

    bank_cfg = get_bank_config(bank)
    bank_prompt = bank_cfg["prompt"]

    # 构建 doc_id → bank 映射（用于过滤结果）
    db = get_db()
    bank_map = {}
    try:
        rows = db.execute("SELECT doc_id, bank FROM doc_meta").fetchall()
        bank_map = {r["doc_id"]: r["bank"] for r in rows}
    except sqlite3.OperationalError:
        pass
    db.close()

    # 始终从 kb bank 召回，提高 limit 以增加覆盖
    raw_results = await recall(q, limit=30, bank="kb")

    # ── 清洗 + 过滤 + 去重合并 ──
    import re
    doc_facts = {}  # doc_id → [(text, doc_name, cleaned_text), ...]
    
    for r in raw_results:
        text = r.get("text", "") or ""
        tags = r.get("tags", [])
        
        # 提取 doc_id
        doc_id = None
        for t in tags:
            if t.startswith("doc_id:"):
                doc_id = t[7:]
                break
        if not doc_id:
            # 技术类 bank 的记忆缺少 doc_id tag，不能丢弃
            if bank in ("tech", "security", "ai", "notes"):
                doc_id = f"_notag_{id(r)}"  # 伪造唯一 ID 用于分组
            else:
                continue
        # 过滤 skip bank
        if bank_map.get(doc_id) == "skip":
            continue
        # 过滤指定 bank
        if bank != "all" and bank_map.get(doc_id) and bank_map.get(doc_id) != bank:
            continue

        # 提取文档名
        doc_name = "未知文档"
        for t in tags:
            if t.startswith("title:"):
                doc_name = t[6:]
                break
        
        # 清理 Hindsight 元数据（| When: ... | Involving: ...）
        cleaned = re.sub(r'\s*\|\s*(When|Involving|Entities|Location|Type|Source):[^|]*', '', text).strip()
        if not cleaned:
            cleaned = text.strip()
        
        if doc_id not in doc_facts:
            doc_facts[doc_id] = []
        doc_facts[doc_id].append((text, doc_name, cleaned))

    if not doc_facts:
        return {"answer": "知识库中未找到相关信息。", "sources": []}

    # ── 按文档合并：每个文档取 top-2 fact，拼接 ──
    context_parts = []
    sources = []
    
    for doc_id, facts in doc_facts.items():
        # 取前 2 个 fact
        top_facts = facts[:2]
        doc_name = top_facts[0][1]
        
        # 合并同一文档的 fact（去重）
        seen_texts = set()
        merged = []
        for _, _, cleaned in top_facts:
            key = cleaned[:80]
            if key not in seen_texts:
                seen_texts.add(key)
                merged.append(cleaned)
        
        if not merged:
            continue
        
        combined = "；".join(merged)
        context_parts.append(f"[来源: {doc_name}]\n{combined}")
        
        # 来源展示：取第一条的原文摘要
        sources.append({
            "doc": doc_name,
            "chunk": f"{len(facts)} 条相关",
            "text": facts[0][0][:200],
        })
    
    # ── 限制 context 总量 ──
    total_chars = sum(len(p) for p in context_parts)
    if total_chars > 8000:
        # 按序截断
        kept = []
        chars = 0
        for p in context_parts:
            if chars + len(p) > 8000:
                break
            kept.append(p)
            chars += len(p)
        context_parts = kept
    
    context = "\n\n---\n\n".join(context_parts)
    sources = sources[:8]

    prompt = f"""{bank_prompt}

基于以下文档内容回答问题。如果文档中没有相关信息，请如实说明。回答尽量详细，引用具体条款和数据。

文档内容：
{context}

问题：{q}

请用中文回答，并在答案中标注信息来源（文档名称）。"""

    try:
        answer = await deepseek_chat([
            {"role": "system", "content": bank_prompt},
            {"role": "user", "content": prompt},
        ])
    except Exception as e:
        answer = f"答案生成失败: {e}"

    return {"answer": answer, "sources": sources}


@app.get("/api/documents")
async def list_documents(bank: str = "all"):
    """列出文档（meta.db 为主，Hindsight 补充 chunk/size）"""
    db = get_db()
    
    # 从 meta.db 查文档（源数据）
    if bank == "all":
        meta_rows = db.execute(
            "SELECT doc_id, title, category, filename, bank, created_at FROM doc_meta WHERE bank NOT IN ('skip') ORDER BY created_at DESC"
        ).fetchall()
    else:
        meta_rows = db.execute(
            "SELECT doc_id, title, category, filename, bank, created_at FROM doc_meta WHERE bank = ? ORDER BY created_at DESC",
            (bank,)
        ).fetchall()
    db.close()
    
    # 从 Hindsight 补充 chunk/size 数据
    result = await hindsight_request(f"/v1/default/banks/kb/documents?limit=1000", timeout=15)
    hs_stats = {}  # doc_id → {chunks, size}
    for item in result.get("items", []):
        doc_id = None
        for t in item.get("tags", []):
            if t.startswith("doc_id:"):
                doc_id = t[7:]
                break
        if not doc_id:
            continue
        if doc_id not in hs_stats:
            hs_stats[doc_id] = {"chunks": 0, "size": 0}
        hs_stats[doc_id]["chunks"] += 1
        hs_stats[doc_id]["size"] += item.get("text_length", 0)
    
    docs = []
    for r in meta_rows:
        stats = hs_stats.get(r["doc_id"], {"chunks": 0, "size": 0})
        docs.append({
            "id": r["doc_id"],
            "title": r["title"] or "未知文档",
            "category": r["category"] or "",
            "filename": r["filename"] or "",
            "chunks": stats["chunks"],
            "size_chars": stats["size"],
            "created": r["created_at"] or "",
            "bank": r["bank"] or "kb",
        })
    
    return {"documents": docs}


@app.patch("/api/documents/{doc_id}")
async def patch_document(doc_id: str, title: str = Form(None), category: str = Form(None)):
    """编辑文档的标题和分类"""
    if not title and not category:
        raise HTTPException(400, "至少需要提供 title 或 category")
    update_meta(doc_id, title=title, category=category)
    return {"ok": True, "doc_id": doc_id, "title": title, "category": category}


@app.patch("/api/documents/{doc_id}/bank")
async def patch_document_bank(doc_id: str, bank: str = Form(...)):
    """切换文档所属 bank"""
    if bank not in BANKS:
        raise HTTPException(400, f"无效的 bank: {bank}，有效值: {', '.join(BANKS.keys())}")
    db = get_db()
    row = db.execute("SELECT doc_id FROM doc_meta WHERE doc_id = ?", (doc_id,)).fetchone()
    if not row:
        db.close()
        raise HTTPException(404, f"文档 {doc_id} 不存在")
    db.execute("UPDATE doc_meta SET bank = ? WHERE doc_id = ?", (bank, doc_id))
    db.commit()
    db.close()
    return {"ok": True, "doc_id": doc_id, "bank": bank}


@app.get("/api/categories")
async def list_categories():
    """列出所有分类及文档数"""
    db = get_db()
    rows = db.execute(
        "SELECT category, COUNT(*) as cnt FROM doc_meta WHERE category != '' GROUP BY category ORDER BY cnt DESC"
    ).fetchall()
    db.close()

    used = {r["category"]: r["cnt"] for r in rows}
    result = []
    for cat in DEFAULT_CATEGORIES:
        result.append({"name": cat, "count": used.get(cat, 0)})
    for cat, cnt in used.items():
        if cat not in DEFAULT_CATEGORIES:
            result.append({"name": cat, "count": cnt})
    return {"categories": result}


@app.get("/api/banks")
async def list_banks():
    """列出所有 bank 及文档统计"""
    db = get_db()
    bank_stats = {}
    try:
        rows = db.execute("SELECT bank, COUNT(*) as cnt FROM doc_meta WHERE bank != 'skip' GROUP BY bank").fetchall()
        bank_stats = {r["bank"]: r["cnt"] for r in rows}
    except sqlite3.OperationalError:
        pass
    db.close()

    total = sum(bank_stats.get(key, 0) for key in BANKS if key != "all")
    banks = []
    for key, cfg in BANKS.items():
        if key == "all":
            banks.append({"key": key, "name": cfg["name"], "count": total})
        else:
            banks.append({"key": key, "name": cfg["name"], "count": bank_stats.get(key, 0)})
    return {"banks": banks}


@app.get("/api/stats")
async def stats():
    """知识库统计"""
    result = await hindsight_request(f"/v1/default/banks/{KB_BANK}/stats")
    return {
        "total_nodes": result.get("total_nodes", 0),
        "total_documents": result.get("total_documents", 0),
        "total_links": result.get("total_links", 0),
    }


@app.delete("/api/documents/{doc_id}")
async def delete_document(doc_id: str):
    """删除文档及其所有向量（按 doc_id tag 查找 Hindsight 内部文档）"""
    # 1. 获取所有 Hindsight 文档，找出带 doc_id:{doc_id} tag 的
    try:
        all_docs = await hindsight_request(
            f"/v1/default/banks/{KB_BANK}/documents?limit=500",
            timeout=15,
        )
    except Exception as e:
        print(f"获取 Hindsight 文档列表失败: {e}", file=sys.stderr)
        all_docs = {"items": []}
    
    # 2. 按 tag 匹配，删除所有关联的 Hindsight 内部文档
    deleted_hs = 0
    for item in all_docs.get("items", []):
        tags = item.get("tags", [])
        for t in tags:
            if t == f"doc_id:{doc_id}":
                try:
                    await hindsight_request(
                        f"/v1/default/banks/{KB_BANK}/documents/{item['id']}",
                        "DELETE",
                        timeout=10,
                    )
                    deleted_hs += 1
                except Exception as e:
                    print(f"删除 Hindsight 文档 {item['id'][:16]} 失败: {e}", file=sys.stderr)
                break  # 一个 Hindsight 文档只删一次
    
    # 3. 删除 SQLite 元数据
    delete_meta(doc_id)
    return {"ok": True, "deleted_hindsight_docs": deleted_hs}


@app.post("/api/reparse/{doc_id}")
async def reparse_document(doc_id: str):
    """重新解析已入库文档：删除旧向量 → 重新OCR → 重新入库
    
    用于升级到 MinerU 后对旧 tesseract 文档重新处理。
    需要文档对应的原始文件在 uploads/ 目录下。
    """
    # 查找文档元数据
    meta = get_meta(doc_id)
    if not meta or not meta.get("filename"):
        raise HTTPException(404, f"文档 {doc_id} 不存在或无文件名记录")
    
    filename = meta["filename"]
    doc_title = meta.get("title", filename_to_title(filename))
    doc_category = meta.get("category", "")
    
    # 查找原始文件
    upload_dir = os.path.join(BASE_DIR, "uploads")
    file_path = os.path.join(upload_dir, filename)
    
    if not os.path.exists(file_path):
        # 尝试直接从 Hindsight tags 重建（PDF 文件可能未保留）
        raise HTTPException(
            404,
            f"原始文件 {filename} 不在 uploads/ 目录中。"
            f"请重新上传文件以触发 MinerU 解析。"
        )
    
    # 读取文件
    with open(file_path, "rb") as f:
        content = f.read()
    
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(400, f"文件过大（{len(content)//1024//1024}MB），上限 {MAX_FILE_SIZE//1024//1024}MB")
    
    # 重新解析
    try:
        text = await parse_document(filename, content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        raise HTTPException(500, f"重新解析异常: {e}")
    
    if not text or len(text.strip()) < 10:
        raise HTTPException(400, "重新解析后文档内容过短")
    
    # 删除旧向量
    try:
        await hindsight_request(
            f"/v1/default/banks/{KB_BANK}/documents/{doc_id}",
            "DELETE",
            timeout=30,
        )
        print(f"已删除旧文档向量: {doc_id}", file=sys.stderr)
    except Exception as e:
        print(f"删除旧向量失败（继续）: {e}", file=sys.stderr)
    
    # 重新切片入库（复用 upload 后的逻辑）
    chunk_size = 1000
    chunks = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]
    new_doc_id = str(uuid.uuid4())
    
    memory_items = []
    for i, chunk in enumerate(chunks):
        chunk = chunk.strip()
        if not chunk:
            continue
        tags = [
            f"doc:{filename}",
            f"chunk:{i+1}/{len(chunks)}",
            f"doc_id:{new_doc_id}",
            f"title:{doc_title}",
        ]
        if doc_category:
            tags.append(f"cat:{doc_category}")
        memory_items.append({"content": chunk, "tags": tags, "type": "world"})
    
    retained = 0
    if memory_items:
        dyn_timeout = max(120, min(len(memory_items) * 5, 600))
        print(f"Reparse: {len(memory_items)} chunks, timeout={dyn_timeout}s", file=sys.stderr)
        try:
            result = await hindsight_request(
                f"/v1/default/banks/{KB_BANK}/memories",
                "POST",
                {"items": memory_items},
                timeout=dyn_timeout,
            )
            retained = result.get("items_count", len(memory_items))
        except Exception as e:
            raise HTTPException(502, f"重新入库失败: {e}")
    
    # 更新元数据（保留旧标题和分类）
    content_hash = hashlib.sha256(content).hexdigest()
    save_meta(new_doc_id, doc_title, doc_category, filename, content_hash)
    
    return {
        "ok": True,
        "old_doc_id": doc_id,
        "new_doc_id": new_doc_id,
        "title": doc_title,
        "chunks": retained,
        "total_chars": len(text),
        "preview": text[:200] + ("..." if len(text) > 200 else ""),
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX_HTML)


# ─── 前端 ─────────────────────────────────────────────────────

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>知识库</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;color:#222;min-height:100vh}
.header{background:#1a1a2e;color:#fff;padding:14px 24px;display:flex;align-items:center;justify-content:space-between}
.header h1{font-size:17px;font-weight:600}
.stats{font-size:12px;color:#aab;display:flex;gap:14px}
.bank-bar{display:flex;background:#fff;border-bottom:1px solid #e0e0e0;padding:8px 16px;align-items:center;gap:8px}
.bank-bar label{font-size:12px;color:#888;white-space:nowrap}
.bank-bar select{padding:6px 10px;border:1px solid #ddd;border-radius:6px;font-size:13px;outline:none;background:#fff;min-width:120px}
.bank-bar select:focus{border-color:#e94560}
.doc-bank-select{font-size:11px;padding:2px 4px;border:1px solid #ddd;border-radius:4px;background:#fff;color:#666;outline:none;max-width:80px}
.doc-bank-select:focus{border-color:#e94560}
.main-area{max-width:900px;margin:0 auto;padding:20px}
.search-box{display:flex;gap:8px;margin-bottom:16px}
.search-box input{flex:1;padding:12px 16px;border:1px solid #ddd;border-radius:8px;font-size:15px;outline:none}
.search-box input:focus{border-color:#e94560}
.search-box button{padding:12px 24px;background:#e94560;color:#fff;border:none;border-radius:8px;font-size:15px;cursor:pointer;font-weight:600}
.search-box button:hover{background:#d63850}
.answer{background:#fff;border-radius:10px;padding:20px;line-height:1.8;font-size:15px;margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.answer .bank-label{display:inline-block;padding:2px 10px;border-radius:10px;font-size:11px;margin-bottom:10px;color:#fff}
.answer .bank-label.all{background:#666}
.answer .bank-label.tech{background:#8e44ad}
.answer .bank-label.security{background:#c0392b}
.answer .bank-label.ai{background:#2980b9}
.answer .bank-label.notes{background:#16a085}
.answer .bank-label.proposals{background:#4a90d9}
.answer .bank-label.assessment{background:#e67e22}
.answer .bank-label.projects{background:#27ae60}
.sources{margin-top:16px;padding-top:12px;border-top:1px solid #eee}
.source{background:#f8f8f8;border-radius:6px;padding:10px 14px;margin-bottom:8px;font-size:13px}
.source .doc{color:#e94560;font-weight:600;margin-bottom:4px}
.source .text{color:#666;line-height:1.6}
.section-bar{display:flex;gap:8px;margin-bottom:16px;align-items:center}
.section-btn{padding:8px 16px;border-radius:6px;font-size:13px;cursor:pointer;border:1px solid #ddd;background:#fff;color:#666;transition:.2s}
.section-btn:hover,.section-btn.active{border-color:#e94560;color:#e94560}
.upload-form{background:#fff;border-radius:12px;padding:24px;margin-bottom:16px;display:none;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.upload-form.show{display:block}
.form-group{margin-bottom:14px}
.form-group label{display:block;font-size:13px;font-weight:600;color:#555;margin-bottom:6px}
.form-group input,.form-group select{width:100%;padding:12px 14px;border:1px solid #ddd;border-radius:8px;font-size:14px;outline:none}
.form-group select{appearance:none;background:#fff url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23888' d='M6 8L1 3h10z'/%3E%3C/svg%3E") no-repeat right 14px center;padding-right:36px}
.upload-zone{border:2px dashed #ccc;border-radius:12px;padding:30px;text-align:center;cursor:pointer;transition:.2s;margin-bottom:16px}
.upload-zone:hover,.upload-zone.dragover{border-color:#e94560;background:#fff5f5}
.upload-zone .icon{font-size:36px;margin-bottom:8px}
.upload-zone .hint{color:#999;font-size:13px}
.upload-zone .file-selected{color:#e94560;font-weight:600;font-size:14px}
.upload-zone input[type=file]{display:none}
.upload-btn{width:100%;padding:14px;background:#e94560;color:#fff;border:none;border-radius:8px;font-size:15px;cursor:pointer;font-weight:600}
.upload-btn:hover{background:#d63850}
.upload-btn:disabled{background:#ccc;cursor:not-allowed}
.result-msg{margin-top:12px;padding:12px;border-radius:6px;font-size:14px}
.result-msg.success{background:#e8f5e9;color:#2e7d32}
.result-msg.error{background:#ffebee;color:#c62828}
.doc-list{background:#fff;border-radius:10px;display:none;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.doc-list.show{display:block}
.doc-item{display:flex;justify-content:space-between;align-items:flex-start;padding:14px 16px;border-bottom:1px solid #f0f0f0;gap:12px}
.doc-item:last-child{border-bottom:none}
.doc-item .main{flex:1;min-width:0}
.doc-item .title-line{display:flex;align-items:center;gap:8px;margin-bottom:4px}
.doc-item .title{font-weight:600;font-size:14px;color:#222}
.doc-item .cat-tag{padding:2px 8px;border-radius:10px;font-size:11px;background:#f0f0f0;color:#888;white-space:nowrap}
.doc-item .meta{color:#999;font-size:12px}
.doc-item .actions{display:flex;gap:6px;flex-shrink:0}
.doc-item .actions span{cursor:pointer;padding:4px 8px;border-radius:4px;font-size:13px}
.doc-item .del{color:#c62828}
.doc-item .del:hover{background:#ffebee}
.loading{text-align:center;padding:20px;color:#999}
.empty{text-align:center;padding:40px;color:#bbb;font-size:14px}
.spin{display:inline-block;width:16px;height:16px;border:2px solid #ddd;border-top-color:#e94560;border-radius:50%;animation:spin .6s linear infinite;margin-right:8px}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="header">
  <h1>📚 知识库</h1>
  <div class="stats" id="stats">加载中...</div>
</div>
<div class="bank-bar" id="bank-bar">
  <label>📂 知识库分类：</label>
  <select id="bank-selector" onchange="switchBank(this.value)"></select>
</div>

<div class="main-area">
  <div class="search-box">
    <input id="query" placeholder="输入问题搜索知识库..." onkeydown="if(event.key==='Enter')doSearch()">
    <button onclick="doSearch()">搜索</button>
  </div>
  <div id="answer-area"></div>

  <div class="section-bar">
    <button class="section-btn active" id="btn-upload" onclick="toggleSection('upload')">📤 上传文档</button>
    <button class="section-btn" id="btn-docs" onclick="toggleSection('docs')">📋 文档列表</button>
  </div>

  <div class="upload-form" id="upload-form">
    <div class="form-group">
      <label>📌 文档标题 <span style="color:#999;font-weight:400">（可选，默认使用文件名）</span></label>
      <input id="upload-title" placeholder="输入文档标题...">
    </div>
    <div class="form-group">
      <label>📂 分类</label>
      <select id="upload-category"><option value="">-- 未分类 --</option></select>
    </div>
    <div class="form-group">
      <label>🏷️ 知识库分类</label>
      <select id="upload-bank"><option value="">-- 加载中 --</option></select>
    </div>
    <div class="upload-zone" id="drop-zone" onclick="document.getElementById('file-input').click()">
      <div class="icon">📄</div>
      <div class="hint" id="drop-hint">点击或拖拽上传文档<br>支持 PDF / Word / Markdown / TXT</div>
      <input type="file" id="file-input" accept=".pdf,.docx,.doc,.md,.txt" onchange="onFileSelected(this.files[0])">
    </div>
    <button class="upload-btn" id="upload-btn" disabled onclick="doUpload()">上传到知识库</button>
    <div id="upload-result"></div>
  </div>

  <div class="doc-list" id="doc-list"></div>
</div>

<script>
let currentBank = 'all';
let selectedFile = null;
let allDocs = [];
let bankData = [];

async function init() {
  await loadBanks();
  await loadCategories();
  loadStats();
}
init();

async function loadBanks() {
  try {
    const r = await fetch('/api/banks');
    bankData = (await r.json()).banks;
    // Populate main bank selector
    const sel = document.getElementById('bank-selector');
    sel.innerHTML = bankData.map(b => 
      `<option value="${b.key}"${b.key === 'all' ? ' selected' : ''}>${b.name} (${b.count || 0})</option>`
    ).join('');
    // Populate upload bank selector
    const upSel = document.getElementById('upload-bank');
    upSel.innerHTML = '<option value="">-- 选择分类 --</option>' +
      bankData.filter(b => b.key !== 'all').map(b =>
        `<option value="${b.key}">${b.name} (${b.count || 0})</option>`
      ).join('');
    // Populate doc row bank dropdowns if visible
    if (document.getElementById('doc-list').classList.contains('show')) {
      populateDocBankSelects();
    }
  } catch(e) {}
}

function switchBank(bank) {
  currentBank = bank;
  document.getElementById('bank-selector').value = bank;
  document.getElementById('answer-area').innerHTML = '';
  document.getElementById('query').value = '';
  document.getElementById('query').focus();
  if (document.getElementById('doc-list').classList.contains('show')) {
    loadDocs();
  }
}

async function doSearch() {
  const q = document.getElementById('query').value.trim();
  if (!q) return;
  const area = document.getElementById('answer-area');
  area.innerHTML = '<div class="loading"><span class="spin"></span>搜索中...</div>';
  
  try {
    const fd = new FormData(); fd.append('q', q); fd.append('bank', currentBank);
    const r = await fetch('/api/query', {method:'POST', body:fd});
    const d = await r.json();
    const bankName = (bankData.find(b => b.key === currentBank) || {}).name || currentBank;
    let html = '<div class="answer"><span class="bank-label '+currentBank+'">'+bankName+'</span><br>' + d.answer.replace(/\\n/g,'<br>') + '</div>';
    if (d.sources && d.sources.length) {
      html += '<div class="sources"><strong>📎 参考来源</strong></div>';
      d.sources.forEach(s => {
        html += `<div class="source"><div class="doc">${s.doc} · ${s.chunk}</div><div class="text">${s.text}</div></div>`;
      });
    }
    area.innerHTML = html;
  } catch(e) {
    area.innerHTML = '<div class="result-msg error">搜索失败: '+e.message+'</div>';
  }
}

function toggleSection(name) {
  if (name === 'upload') {
    const form = document.getElementById('upload-form');
    form.classList.toggle('show');
    document.getElementById('btn-upload').classList.toggle('active', form.classList.contains('show'));
    if (form.classList.contains('show')) {
      document.getElementById('doc-list').classList.remove('show');
      document.getElementById('btn-docs').classList.remove('active');
    }
  } else {
    const list = document.getElementById('doc-list');
    list.classList.toggle('show');
    document.getElementById('btn-docs').classList.toggle('active', list.classList.contains('show'));
    if (list.classList.contains('show')) {
      document.getElementById('upload-form').classList.remove('show');
      document.getElementById('btn-upload').classList.remove('active');
      loadDocs();
    }
  }
}

function onFileSelected(file) {
  if (!file) return;
  selectedFile = file;
  document.getElementById('drop-hint').innerHTML = `<span class="file-selected">📎 ${file.name}</span><br><small style="color:#999">${(file.size/1024).toFixed(1)} KB</small>`;
  document.getElementById('upload-btn').disabled = false;
  const titleInput = document.getElementById('upload-title');
  if (!titleInput.value) titleInput.value = file.name.replace(/\\.[^.]+$/, '');
}

async function doUpload() {
  if (!selectedFile) return;
  const btn = document.getElementById('upload-btn');
  const resultDiv = document.getElementById('upload-result');
  btn.disabled = true; btn.textContent = '上传中...';
  resultDiv.innerHTML = '';

  const slowTimer = setTimeout(() => {
    resultDiv.innerHTML = '<div class="result-msg" style="background:#fff3e0;color:#e65100">⏳ 处理中...扫描件 PDF 会进行 OCR 识别，可能需要数十秒</div>';
  }, 10000);

  const fd = new FormData();
  fd.append('file', selectedFile);
  fd.append('title', document.getElementById('upload-title').value.trim());
  fd.append('category', document.getElementById('upload-category').value);
  const uploadBank = document.getElementById('upload-bank').value;
  fd.append('bank', uploadBank || (currentBank === 'all' ? 'kb' : currentBank));
  
  try {
    const r = await fetch('/api/upload', {method:'POST', body:fd});
    clearTimeout(slowTimer);
    const d = await r.json();
    if (r.status === 409) {
      resultDiv.innerHTML = '<div class="result-msg" style="background:#fff3e0;color:#e65100;white-space:pre-line">⚠️ ' + (d.detail || '文档已存在') + '</div>';
      btn.disabled = false; btn.textContent = '上传到知识库'; return;
    }
    if (d.ok) {
      let msg = `<div class="result-msg success">✅ <b>${d.title}</b> 已入库 · ${d.chunks} 个片段 · ${d.total_chars} 字符<br><small>预览: ${d.preview}</small>`;
      if (d.warning) msg += `<br><small style="color:#e67e22">⚠️ ${d.warning}</small>`;
      msg += '</div>';
      resultDiv.innerHTML = msg;
      selectedFile = null;
      document.getElementById('upload-title').value = '';
      document.getElementById('drop-hint').innerHTML = '点击或拖拽上传文档<br>支持 PDF / Word / Markdown / TXT';
      loadStats(); loadBanks();
    } else {
      resultDiv.innerHTML = '<div class="result-msg error">❌ 上传失败' + (d.detail ? ': ' + d.detail : '') + '</div>';
    }
  } catch(e) {
    clearTimeout(slowTimer);
    resultDiv.innerHTML = '<div class="result-msg error">上传失败: ' + (e.message === 'Failed to fetch' ? '网络超时或服务未响应' : e.message) + '</div>';
  }
  btn.disabled = false; btn.textContent = '上传到知识库';
}

async function loadDocs() {
  const list = document.getElementById('doc-list');
  list.innerHTML = '<div class="loading"><span class="spin"></span>加载中...</div>';
  try {
    const r = await fetch('/api/documents?bank=' + currentBank);
    const d = await r.json();
    allDocs = d.documents;
    renderDocs();
  } catch(e) {
    list.innerHTML = '<div class="result-msg error">加载失败</div>';
  }
}

function renderDocs() {
  const list = document.getElementById('doc-list');
  if (!allDocs.length) {
    list.innerHTML = '<div class="empty">暂无文档</div>'; return;
  }
  list.innerHTML = allDocs.map(doc => {
    const catHtml = doc.category ? `<span class="cat-tag">${doc.category}</span>` : '';
    const dateStr = doc.created ? new Date(doc.created).toLocaleDateString('zh-CN') : '-';
    const bankOpts = bankData.filter(b => b.key !== 'all').map(b =>
      `<option value="${b.key}"${doc.bank === b.key ? ' selected' : ''}>${b.name}</option>`
    ).join('');
    return `<div class="doc-item" id="doc-row-${doc.id}">
      <div class="main">
        <div class="title-line">
          <span class="title">📄 ${doc.title}</span>${catHtml}
        </div>
        <div class="meta">${doc.chunks > 0 ? doc.chunks + ' 片段 · ' + doc.size_chars + ' 字符' : '—'} · ${dateStr}</div>
      </div>
      <div class="actions">
        <select class="doc-bank-select" onchange="changeDocBank('${doc.id}', this.value)" title="切换分类">${bankOpts}</select>
        <span class="del" onclick="delDoc('${doc.id}',this)">🗑</span>
      </div>
    </div>`;
  }).join('');
}

function populateDocBankSelects() {
  document.querySelectorAll('.doc-bank-select').forEach(sel => {
    const currentVal = sel.value;
    sel.innerHTML = bankData.filter(b => b.key !== 'all').map(b =>
      `<option value="${b.key}"${b.key === currentVal ? ' selected' : ''}>${b.name}</option>`
    ).join('');
  });
}

async function changeDocBank(docId, newBank) {
  if (!newBank || newBank === 'all') return;
  try {
    const fd = new FormData(); fd.append('bank', newBank);
    const r = await fetch('/api/documents/' + docId + '/bank', {method:'PATCH', body:fd});
    if (r.ok) {
      // Update local data
      const doc = allDocs.find(d => d.id === docId);
      if (doc) doc.bank = newBank;
      loadBanks();
    }
  } catch(e) {}
}

async function delDoc(id, el) {
  if (!confirm('确认删除此文档及所有关联内容？')) return;
  await fetch('/api/documents/'+id, {method:'DELETE'});
  el.closest('.doc-item').remove();
  allDocs = allDocs.filter(d => d.id !== id);
  loadStats(); loadBanks();
}

let catData = [];
async function loadCategories() {
  try {
    const r = await fetch('/api/categories');
    catData = (await r.json()).categories;
    const sel = document.getElementById('upload-category');
    if (sel.options.length <= 1) {
      catData.forEach(c => { if (c.name !== '其他') sel.appendChild(new Option(c.name, c.name)); });
      sel.appendChild(new Option('其他', '其他'));
    }
  } catch(e) {}
}

async function loadStats() {
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();
    document.getElementById('stats').innerHTML = `<span>📄 ${d.total_documents||0} 文档</span><span>🧩 ${d.total_nodes||0} 节点</span>`;
  } catch(e) {}
}

const dropZone = document.getElementById('drop-zone');
dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('dragover'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
dropZone.addEventListener('drop', e => {
  e.preventDefault(); dropZone.classList.remove('dragover');
  const file = e.dataTransfer.files[0];
  if (file) onFileSelected(file);
});
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3002)
