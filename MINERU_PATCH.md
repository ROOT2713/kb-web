# MinerU API 集成 Patch 规格

## 概述
将 kb-web/server.py 的 PDF OCR 引擎从 tesseract 升级为 MinerU API（上海AI Lab）。
实测对比：中文表格恢复率 0% → 100%，详见表5-48验收测试费率表。

## 环境准备（CC手动执行）
```bash
echo 'MINERU_API_TOKEN=eyJ0eXBlIjoiSldUIiwiYWxnIjoiSFM1MTIifQ.eyJqdGkiOiI4MjYwNzIxOCIsInJvbCI6IlJPTEVfUkVHSVNURVIiLCJpc3MiOiJPcGVuWExhYiIsImlhdCI6MTc3OTQzMTAwNSwiY2xpZW50SWQiOiJsa3pkeDU3bnZ5MjJqa3BxOXgydyIsInBob25lIjoiIiwib3BlbklkIjpudWxsLCJ1dWlkIjoiM2U5YmJlNzMtMDJkNC00MWNmLTg1YTgtMDY0YTlmNjk2NmQzIiwiZW1haWwiOiIiLCJleHAiOjE3ODcyMDcwMDV9.f6J0JjVWAcpuPUXA-4sJOkhL6Ht_knFzLsv3Irr4yt7QAUsBLBgkXuAI96yq-7Y0m4ZPgaWQpX3muVuoh5taSg' >> ~/.hermes/.env
```

---

## 改动清单

### 1. 头部导入区（第7-16行之间）

在第10行 `from io import BytesIO` 之后插入：
```python
import zipfile as _zipfile
import time as _time
```

### 2. 配置常量区（第31行之后）

在第31行 `KB_BANK = "kb"` 之后插入：
```python
# MinerU API 配置
MINERU_TOKEN = os.environ.get("MINERU_API_TOKEN", "")
MINERU_BASE = "https://mineru.net/api/v4"
MINERU_PAGES_MAX = 200  # 单次API调用的最大页数
```

### 3. MAX_FILE_SIZE（第114行）

```python
# 改前:
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

# 改后:
MAX_FILE_SIZE = 200 * 1024 * 1024  # 200MB（与 MinerU 对齐）
```

### 4. ocr_pdf() 参数优化（第128-158行）

作为 fallback 路径，优化 tesseract 参数：

第131行：
```python
# 改前:
["pdftoppm", "-png", "-gray", "-r", "150", ...]

# 改后:
["pdftoppm", "-png", "-gray", "-r", "200", ...]
```

第132行：
```python
# 改前:
timeout=300

# 改后:
timeout=600
```

第158行：
```python
# 改前:
["tesseract", str(png), out_base, "-l", "chi_sim+eng", "--psm", "6"],

# 改后:
["tesseract", str(png), out_base, "-l", "chi_sim+eng", "--psm", "3"],
```

### 5. 新增 mineru_parse_pdf() 函数

在 `ocr_pdf()` 函数之后（第178行之后，`parse_document` 之前）插入：

```python
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
```

### 6. 修改 parse_document() PDF 分支（第180-217行）

```python
# 改前 (第180-217行):
def parse_document(filename: str, content: bytes) -> str:
    """解析 PDF/Word/Markdown/TXT → 纯文本（扫描件 PDF 自动 OCR）"""
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
            # 文字层为空 → 尝试 OCR
            print(f"PDF 文字层为空，启动 OCR（{len(reader.pages)} 页）...", file=sys.stderr)
            try:
                text = ocr_pdf(content)
                if not text.strip():
                    raise ValueError(
                        "PDF OCR 识别结果为空。"
                        "可能原因：①图片质量过低 ②PDF 为纯图片且文字不清晰。"
                    )
                print(f"OCR 完成，提取 {len(text)} 字符", file=sys.stderr)
            except RuntimeError as e:
                raise ValueError(f"PDF OCR 失败: {e}")
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                raise ValueError(f"PDF OCR 异常: {e}")
        return text

# 改后:
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
```

### 7. 修改 upload() 端点（第284行）

因为 `parse_document` 变成了 async，需要在调用处加 await：

第302行：
```python
# 改前:
text = parse_document(file.filename, content)

# 改后:
text = await parse_document(file.filename, content)
```

### 8. 新增 /api/reparse 端点

提供对已入库文档重新走 MinerU 解析的能力。

在第553行（`delete_document` 路由之后，`@app.get("/")` 之前）插入：

```python
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
        try:
            result = await hindsight_request(
                f"/v1/default/banks/{KB_BANK}/memories",
                "POST",
                {"items": memory_items},
                timeout=120,
            )
            retained = result.get("items_count", len(memory_items))
        except Exception as e:
            raise HTTPException(502, f"重新入库失败: {e}")
    
    # 更新元数据（保留旧标题和分类）
    save_meta(new_doc_id, doc_title, doc_category, filename)
    
    return {
        "ok": True,
        "old_doc_id": doc_id,
        "new_doc_id": new_doc_id,
        "title": doc_title,
        "chunks": retained,
        "total_chars": len(text),
        "preview": text[:200] + ("..." if len(text) > 200 else ""),
    }
```

---

## 改动总结

| 改动 | 类型 | 行范围 |
|------|------|--------|
| 新增 import zipfile, time | 添加 | ~L10 |
| 新增 MINERU_TOKEN, MINERU_BASE, MINERU_PAGES_MAX | 添加 | ~L32 |
| MAX_FILE_SIZE 50→200MB | 修改 | L114 |
| ocr_pdf() DPI 150→200, psm 6→3, timeout 300→600 | 修改 | L131,132,158 |
| 新增 mineru_parse_pdf() + _asleep() (~100行) | 添加 | ~L178 后 |
| parse_document() → async, PDF优先MinerU | 修改 | L180-217 |
| upload() 调用处加 await | 修改 | L302 |
| 新增 /api/reparse/{doc_id} 端点 (~70行) | 添加 | ~L553 后 |

## 验证方法

1. 重启服务: `sudo systemctl restart kb-web`
2. 上传一个扫描件PDF → 应走 MinerU
3. 搜索表格数据 → 应能命中
4. 检查日志: `journalctl -u kb-web -f` | grep MinerU
