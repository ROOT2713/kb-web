#!/usr/bin/env python3
"""Test script for adaptive chunking functions: profile_document and heading_chunk."""
import sys
sys.path.insert(0, "/home/ubuntu/kb-web")

# Import the functions from server.py
from server import profile_document, heading_chunk, parent_child_chunk

# ── Test 1: GB Standard with markdown headings ──
print("=" * 60)
print("TEST 1: GB Standard (markdown headings)")
print("=" * 60)

gb_text = """GB/T 28449-2018 信息安全技术 网络安全等级保护测评过程指南

## 1 范围

本标准规定了网络安全等级保护测评过程的要求，包括测评准备、方案编制、现场测评、分析与报告四个阶段。

## 2 规范性引用文件

下列文件对于本文件的应用是必不可少的。

GB/T 28448-2019 信息安全技术 网络安全等级保护测评要求

## 3 术语和定义

下列术语和定义适用于本文件。

### 3.1 测评 assessment

依据相关技术标准和规范，对信息系统安全等级保护状况进行检测评估的活动。

### 3.2 测评机构 assessment institution

具有相应资质，从事等级测评活动的机构。

## 4 总则

等级测评工作应遵循客观公正、科学规范的原则。测评过程应按照测评准备、方案编制、现场测评、分析与报告四个阶段进行。

## 5 测评准备

### 5.1 测评准备阶段的目标

测评准备阶段的目标是为方案编制和现场测评做好各项准备工作。

### 5.2 测评准备阶段的活动

测评准备阶段的主要活动包括：项目启动、信息收集和分析、测评方案编制等。

### 5.2.1 项目启动

项目启动活动包括组建测评项目组、指定项目负责人等。

### 5.2.2 信息收集和分析

信息收集和分析活动包括收集被测系统相关信息，分析系统安全保护等级等。

## 6 方案编制

### 6.1 方案编制阶段的目标

方案编制阶段的目标是编制测评方案。

## 附录A 测评报告模板

### A.1 报告封面

测评报告封面应包含被测系统名称、测评机构名称等信息。

### A.2 报告正文

报告正文应包含测评概述、测评对象、测评方法、测评结果等内容。
"""

profile = profile_document(gb_text)
print(f"doc_type: {profile['doc_type']}")
print(f"confidence: {profile['confidence']:.2f}")
print(f"headings count: {len(profile['headings'])}")
for level, title, pos in profile['headings']:
    print(f"  level={level}, title='{title}'")

chunks = heading_chunk(gb_text, profile)
print(f"\nChunking result: {len(chunks)} chunks")
for c in chunks:
    child_preview = c['child'][:60].replace('\n', ' ')
    print(f"  child[{c['child_index']}] parent[{c['parent_index']}] hint='{c['section_hint'][:40]}' text='{child_preview}...'")

# ── Test 2: GB Standard with raw (no markdown) headings ──
print("\n" + "=" * 60)
print("TEST 2: GB Standard (raw text headings, no ##)")
print("=" * 60)

gb_raw = """GB 50348-2018 安全防范工程技术标准

1范围
本标准适用于新建、改建和扩建的安全防范工程。

2规范性引用文件
下列文件是本标准的引用文件。

3术语和定义
3.1 安全防范
3.2 入侵报警系统

4总则
安全防范工程应遵循安全可靠、技术先进、经济适用的原则。

5设计要求
5.1 一般规定
安全防范系统设计应综合考虑防护对象的特点和要求。
5.2 入侵报警系统
入侵报警系统应能及时探测入侵行为。
5.2.1 探测器选择
应根据防护要求选择适当类型的探测器。
5.2.2 系统组网
报警信号传输应可靠。
"""

profile2 = profile_document(gb_raw)
print(f"doc_type: {profile2['doc_type']}")
print(f"confidence: {profile2['confidence']:.2f}")
print(f"headings count: {len(profile2['headings'])}")
for level, title, pos in profile2['headings']:
    print(f"  level={level}, title='{title}'")

chunks2 = heading_chunk(gb_raw, profile2)
print(f"\nChunking result: {len(chunks2)} chunks")
for c in chunks2:
    child_preview = c['child'][:60].replace('\n', ' ')
    print(f"  child[{c['child_index']}] parent[{c['parent_index']}] hint='{c['section_hint'][:40]}' text='{child_preview}...'")

# ── Test 3: Regulation document ──
print("\n" + "=" * 60)
print("TEST 3: Regulation document (article-based)")
print("=" * 60)

regulation_text = """中华人民共和国网络安全法

第一条 为了保障网络安全，维护网络空间主权和国家安全、社会公共利益，制定本法。

第二条 在中华人民共和国境内建设、运营、维护和使用网络，以及网络安全的监督管理，适用本法。

第三条 国家坚持网络安全与信息化发展并重，遵循积极利用、科学发展、依法管理、确保安全的方针。

第四条 国家建立网络安全等级保护制度。

第五条 国家采取措施，监测、防御、处置来源于中华人民共和国境内外的网络安全风险和威胁。

第六条 网络产品、服务应当符合相关国家标准的强制性要求。

第七条 网络运营者应当对其收集的用户信息严格保密。

第八条 网络运营者应当建立健全用户信息保护制度。
"""

profile3 = profile_document(regulation_text)
print(f"doc_type: {profile3['doc_type']}")
print(f"confidence: {profile3['confidence']:.2f}")
print(f"headings count: {len(profile3['headings'])}")

chunks3 = heading_chunk(regulation_text, profile3)
print(f"\nChunking result: {len(chunks3)} chunks")
for c in chunks3:
    child_preview = c['child'][:60].replace('\n', ' ')
    print(f"  child[{c['child_index']}] parent[{c['parent_index']}] hint='{c['section_hint'][:40]}' text='{child_preview}...'")

# ── Test 4: Generic document (no matching patterns) ──
print("\n" + "=" * 60)
print("TEST 4: Generic document (no headings)")
print("=" * 60)

generic_text = """This is a random document with no structured headings. It contains various paragraphs of text about different topics but doesn't follow any standard document format.

The content is just flowing text without any numbered sections or article patterns. It could be a meeting note, a blog post, or any other type of unstructured document.

There's really nothing special about this text at all. It's just meant to test that the generic fallback works correctly.
"""

profile4 = profile_document(generic_text)
print(f"doc_type: {profile4['doc_type']}")
print(f"confidence: {profile4['confidence']:.2f}")

chunks4 = heading_chunk(generic_text, profile4)
print(f"heading_chunk returned: {len(chunks4)} chunks (expected 0, will fall back to parent_child_chunk)")

pc_chunks4 = parent_child_chunk(generic_text)
print(f"parent_child_chunk returned: {len(pc_chunks4)} chunks (fallback)")

# ── Test 5: Mixed appendix patterns ──
print("\n" + "=" * 60)
print("TEST 5: GB Standard with appendix patterns")
print("=" * 60)

appendix_text = """## 1 范围

本标准规定了xxx。

## 5 测试方法

### 5.1 测试环境

测试应在标准环境下进行。

### 5.2 测试步骤

按照以下步骤进行测试。

## 附录A 测试记录表

### A.1 记录表格式

测试记录应按以下格式填写。

### A.2 填写说明

所有项目均应如实填写。

## 附录B 补充要求

### B.1 特殊情况处理

对于特殊情况应单独处理。
"""

profile5 = profile_document(appendix_text)
print(f"doc_type: {profile5['doc_type']}")
print(f"confidence: {profile5['confidence']:.2f}")
print(f"headings count: {len(profile5['headings'])}")
for level, title, pos in profile5['headings']:
    print(f"  level={level}, title='{title}'")

chunks5 = heading_chunk(appendix_text, profile5)
print(f"\nChunking result: {len(chunks5)} chunks")
for c in chunks5:
    child_preview = c['child'][:60].replace('\n', ' ')
    print(f"  child[{c['child_index']}] parent[{c['parent_index']}] hint='{c['section_hint'][:40]}' text='{child_preview}...'")

print("\n" + "=" * 60)
print("ALL TESTS COMPLETED")
print("=" * 60)
