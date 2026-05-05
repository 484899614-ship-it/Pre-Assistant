import os
import re
import json
import uuid
import base64
import requests
import fitz  # PyMuPDF
from PIL import Image
from flask import Flask, request, jsonify, send_file, send_from_directory
from pptx import Presentation
from pptx.util import Pt, Inches, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE
from io import BytesIO
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# resvg-py 可选
RESVG_AVAILABLE = False
try:
    from resvg_py import svg_to_bytes
    RESVG_AVAILABLE = True
    print("[SVG] resvg-py 可用，支持精美模式")
except ImportError:
    print("[SVG] resvg-py 未安装，精美模式不可用")

app = Flask(__name__)

# ====================== 配置 ======================
DOUBAO_API_KEY = os.environ.get("DOUBAO_API_KEY", "ark-3fc767ed-d625-4d7f-a3f9-1a622fc02d99-fff63")
DOUBAO_ENDPOINT = os.environ.get("DOUBAO_ENDPOINT", "https://ark.cn-beijing.volces.com/api/v3/chat/completions")
MODEL_NAME = os.environ.get("MODEL_NAME", "doubao-seed-2-0-lite-260215")

# PaddleOCR 可选
PADDLEOCR_AVAILABLE = False
_ocr_engine = None
try:
    from paddleocr import PPStructureV3
    PADDLEOCR_AVAILABLE = True
    print("[OCR] PaddleOCR 可用（PPStructureV3），将使用高精度版面分析")
except ImportError:
    try:
        from paddleocr import PPStructure
        PADDLEOCR_AVAILABLE = True
        print("[OCR] PaddleOCR 可用（PPStructure），将使用高精度版面分析")
    except ImportError:
        print("[OCR] PaddleOCR 未安装，使用轻量级图片分类检测")

# 会话存储
sessions = {}


# ====================== 公式检测：轻量级图片分类 ======================
def classify_embedded_images(figures_list, figures_dir):
    """对已提取的嵌入图片按尺寸/比例分类，识别疑似公式。

    启发式规则（基于学术论文 PDF 中公式图片的实际特征）：
    - 宽扁图片（宽高比 > 3 且高度 < 150px） → 疑似公式（最常见：行间公式）
    - 小面积图片（面积 < 100000px² 且不是大正方形） → 疑似公式
    """
    reclassified = []
    eq_count = 0

    for fig in figures_list:
        w, h = fig["width"], fig["height"]
        area = w * h
        aspect = w / h if h > 0 else 999

        is_equation = False
        # 规则1：宽扁图片（行间公式典型特征：很宽、很矮）
        if aspect > 3.0 and h < 150:
            is_equation = True
        # 规则2：小面积且明显扁或窄的图片（排除正方形小图表）
        elif area < 100000 and h < 200 and (aspect > 2.0 or aspect < 0.5):
            is_equation = True
        # 规则3：极小的嵌入式对象（排除正方形）
        elif area < 50000 and h < 100 and aspect > 2.0:
            is_equation = True

        if is_equation:
            eq_count += 1
            # 移动文件从 figures/ 到 equations/
            eq_filename = f"eq_from_fig_{eq_count}.png"
            eq_dir = os.path.join(os.path.dirname(figures_dir), "equations")
            os.makedirs(eq_dir, exist_ok=True)
            eq_path = os.path.join(eq_dir, eq_filename)

            # 复制图片
            try:
                img = Image.open(fig["path"])
                img.save(eq_path)
            except Exception:
                eq_path = fig["path"]  # fallback：不移动

            reclassified.append({
                "index": 0,  # 后面重新编号
                "page": fig["page"],
                "width": w,
                "height": h,
                "text_preview": f"(图片公式, {w}x{h})",
                "filename": eq_filename,
                "path": eq_path,
                "type": "equation"
            })
        else:
            reclassified.append(fig)

    return reclassified, eq_count


# ====================== 公式检测：PaddleOCR 版面分析 ======================
def detect_with_paddleocr(page_img_path, page_num, equations_dir, existing_count):
    """用 PPStructure 进行版面分析，检测公式区域。"""
    if not PADDLEOCR_AVAILABLE:
        return []

    global _ocr_engine
    try:
        if _ocr_engine is None:
            try:
                from paddleocr import PPStructureV3
                _ocr_engine = PPStructureV3()
            except (ImportError, TypeError):
                from paddleocr import PPStructure
                _ocr_engine = PPStructure(show_log=False, image_dir=equations_dir)

        result = _ocr_engine(page_img_path)

        equations = []
        for item in result:
            bbox = item.get("bbox", [])
            category = item.get("type", "")

            # PPStructure 识别出的公式区域
            if category in ("equation", "formula", "isolated", "displayed_equation"):
                x0, y0, x1, y1 = bbox
                # 从页面截图中裁剪该区域
                page_img = Image.open(page_img_path)
                pw, ph = page_img.size
                # bbox 坐标需要 clamp
                x0, y0 = max(0, int(x0)), max(0, int(y0))
                x1, y1 = min(pw, int(x1)), min(ph, int(y1))

                if x1 - x0 < 20 or y1 - y0 < 10:
                    continue

                cropped = page_img.crop((x0, y0, x1, y1))
                eq_idx = existing_count + len(equations) + 1
                eq_filename = f"eq_ocr_p{page_num}_{eq_idx}.png"
                eq_path = os.path.join(equations_dir, eq_filename)
                cropped.save(eq_path)

                equations.append({
                    "index": 0,  # 后面重新编号
                    "page": page_num,
                    "width": x1 - x0,
                    "height": y1 - y0,
                    "text_preview": f"(OCR检测公式, p{page_num})",
                    "filename": eq_filename,
                    "path": eq_path,
                    "type": "equation"
                })

        return equations
    except Exception as e:
        print(f"[OCR] PaddleOCR 第{page_num}页分析失败: {e}")
        return []


# ====================== 第一步：PDF 提取 ======================
def extract_pdf(pdf_path, session_dir):
    """用 PyMuPDF 提取文本、嵌入图片、渲染页面、检测公式区域。"""
    doc = fitz.open(pdf_path)
    pages_text = []
    raw_figures = []   # 原始嵌入图片（未分类）
    equations = []     # 公式区域
    page_images = []   # 整页渲染图路径

    figures_dir = os.path.join(session_dir, "figures")
    pages_dir = os.path.join(session_dir, "pages")
    equations_dir = os.path.join(session_dir, "equations")
    os.makedirs(figures_dir, exist_ok=True)
    os.makedirs(pages_dir, exist_ok=True)
    os.makedirs(equations_dir, exist_ok=True)

    noise_keywords = [
        "We are grateful to", "Author disclosures", "gratefully acknowledge funding",
        "© The Author", "Supplementary Material", "JEL classification",
        "Acknowledgments", "Acknowledgements"
    ]

    # 数学符号集合，用于文本公式检测
    math_symbols = set("∑∏∫∂√∞≈≠≤≥±×÷∈∉⊂⊃∪∩∧∨∃∀⟹⟶→←↔∝∇∆αβγδεζηθλμξπρσφψω")

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_num = page_idx + 1

        # --- 1. 提取文本 ---
        txt = page.get_text("text") or ""
        for kw in noise_keywords:
            pos = txt.find(kw)
            if pos != -1:
                txt = txt[:pos]
        pages_text.append({"page": page_num, "text": txt.strip()})

        # --- 2. 渲染整页为 PNG ---
        pix = page.get_pixmap(dpi=200)
        page_img_path = os.path.join(pages_dir, f"page_{page_num:03d}.png")
        pix.save(page_img_path)
        page_images.append({"page": page_num, "path": page_img_path})

        # --- 3. 提取嵌入图片（暂不分类） ---
        img_list = page.get_images(full=True)
        for img_idx, img_info in enumerate(img_list):
            xref = img_info[0]
            try:
                base_image = doc.extract_image(xref)
                img_bytes = base_image["image"]
                img_ext = base_image.get("ext", "png")
                img_w = base_image.get("width", 0)
                img_h = base_image.get("height", 0)

                if img_w < 30 or img_h < 20:
                    continue

                fig_filename = f"fig_p{page_num}_{img_idx + 1}.{img_ext}"
                fig_path = os.path.join(figures_dir, fig_filename)
                with open(fig_path, "wb") as f:
                    f.write(img_bytes)

                raw_figures.append({
                    "index": 0,  # 后面重新编号
                    "page": page_num,
                    "width": img_w,
                    "height": img_h,
                    "filename": fig_filename,
                    "path": fig_path,
                    "type": "figure"  # 暂定，后面分类可能改为 equation
                })
            except Exception as e:
                print(f"[提取] 第{page_num}页图片提取失败: {e}")

        # --- 4. 文本公式检测（启发式） ---
        blocks = page.get_text("dict")["blocks"]
        for block in blocks:
            if block["type"] != 0:
                continue
            block_text = ""
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    block_text += span.get("text", "")

            block_text = block_text.strip()
            if not block_text or len(block_text) < 3:
                continue

            math_count = sum(1 for c in block_text if c in math_symbols)
            has_equals = "=" in block_text
            has_special = any(c in block_text for c in math_symbols)
            has_hat_sub = bool(re.search(r'[a-zA-Z]_[\{(\w]', block_text))

            is_equation = False
            if math_count >= 2:
                is_equation = True
            elif has_special and has_equals and len(block_text) < 200:
                is_equation = True
            elif has_hat_sub and has_equals and len(block_text) < 200:
                is_equation = True

            if is_equation:
                bbox = fitz.Rect(block["bbox"])
                pad = 8
                bbox = fitz.Rect(
                    max(0, bbox.x0 - pad),
                    max(0, bbox.y0 - pad),
                    min(page.rect.width, bbox.x1 + pad),
                    min(page.rect.height, bbox.y1 + pad)
                )
                page_pix = page.get_pixmap(dpi=250, clip=bbox)
                eq_filename = f"eq_text_p{page_num}_{len(equations) + 1}.png"
                eq_path = os.path.join(equations_dir, eq_filename)
                page_pix.save(eq_path)

                equations.append({
                    "index": 0,
                    "page": page_num,
                    "width": int(bbox.width),
                    "height": int(bbox.height),
                    "text_preview": block_text[:80],
                    "filename": eq_filename,
                    "path": eq_path,
                    "type": "equation"
                })

    total_pages = len(doc)
    doc.close()

    # --- 5. 轻量级图片分类：识别图片公式 ---
    classified, eq_from_fig = classify_embedded_images(raw_figures, figures_dir)

    # 分离出真正的 figures 和 equations
    figures = [f for f in classified if f["type"] == "figure"]
    eq_from_images = [f for f in classified if f["type"] == "equation"]
    equations.extend(eq_from_images)

    # --- 6. PaddleOCR 版面分析（可选增强） ---
    if PADDLEOCR_AVAILABLE:
        print("[OCR] 开始 PaddleOCR 版面分析...")
        for pi in page_images:
            ocr_eqs = detect_with_paddleocr(pi["path"], pi["page"], equations_dir, len(equations))
            equations.extend(ocr_eqs)
        print(f"[OCR] PaddleOCR 检测到 {len(equations) - len(equations) + len(ocr_eqs)} 个公式")

    # --- 7. 去重：同一页面上重叠的公式区域 ---
    equations = deduplicate_equations(equations)

    # --- 8. 统一编号 ---
    for i, fig in enumerate(figures):
        fig["index"] = i + 1
    for i, eq in enumerate(equations):
        eq["index"] = i + 1

    # 检测章节标题
    outline = []
    heading_re = re.compile(r'^(\d+\.?\s+[A-Z][^.]{5,80})', re.MULTILINE)
    full_text = "\n".join(p["text"] for p in pages_text)
    for m in heading_re.finditer(full_text):
        outline.append({"title": m.group(1).strip(), "pos": m.start()})

    visual_assets = figures + equations

    print(f"[提取] 完成: {total_pages}页, {len(figures)}张图, {len(equations)}个公式")

    return {
        "total_pages": total_pages,
        "pages": pages_text,
        "figures": figures,
        "equations": equations,
        "visual_assets": visual_assets,
        "outline": outline,
        "session_dir": session_dir
    }


def deduplicate_equations(equations):
    """去除同一页上位置重叠的公式。"""
    if not equations:
        return equations

    # 按页分组
    by_page = {}
    for eq in equations:
        p = eq["page"]
        if p not in by_page:
            by_page[p] = []
        by_page[p].append(eq)

    result = []
    for page_num, eqs in by_page.items():
        kept = []
        for eq in eqs:
            is_dup = False
            for existing in kept:
                # 如果两个公式来自同一页且预览文本相似，视为重复
                if (eq.get("text_preview") and existing.get("text_preview")
                        and eq["text_preview"] == existing["text_preview"]):
                    is_dup = True
                    break
            if not is_dup:
                kept.append(eq)
        result.extend(kept)

    return result


# ====================== 从 AI 回复提取 JSON ======================
def extract_json_from_reply(reply):
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', reply, re.DOTALL)
    if m:
        return m.group(1)
    m = re.search(r'\{.*\}', reply, re.DOTALL)
    if m:
        return m.group(0)
    return reply


# ====================== 第二步：AI 生成提纲 ======================
def ai_generate_outline(extracted_data):
    """调用 AI 生成文献阅读矩阵和 PPT 提纲方案，包含图表/公式映射。"""
    pages_text = extracted_data["pages"]
    figures = extracted_data["figures"]
    equations = extracted_data["equations"]

    # 拼接论文文本
    text_with_pages = ""
    for p in pages_text:
        if p["text"]:
            text_with_pages += f"\n--- 第{p['page']}页 ---\n{p['text']}\n"

    if len(text_with_pages) > 12000:
        text_with_pages = text_with_pages[:12000] + "\n...（文本过长，已截断）"

    # 构建资源描述（严格按实际提取结果，防止 AI 幻觉）
    assets_desc = ""
    has_figures = len(figures) > 0
    has_equations = len(equations) > 0

    if has_figures:
        assets_desc += "\n### 可用的图表（仅限以下编号）\n" + "\n".join(
            f"  图{f['index']}（第{f['page']}页，{f['width']}x{f['height']}）" for f in figures
        )
    else:
        assets_desc += "\n### 没有检测到图表\n"

    if has_equations:
        assets_desc += "\n### 可用的公式（仅限以下编号）\n" + "\n".join(
            f"  公式{e['index']}（第{e['page']}页）: {e.get('text_preview', '')}" for e in equations
        )
    else:
        assets_desc += "\n### 没有检测到公式\n"

    # 严格约束：列出允许的引用
    allowed_refs = ""
    if has_figures:
        allowed_refs += "图表编号: " + ", ".join(f"图{f['index']}" for f in figures) + "。"
    if has_equations:
        allowed_refs += "公式编号: " + ", ".join(f"公式{e['index']}" for e in equations) + "。"
    if not has_figures and not has_equations:
        allowed_refs = "没有任何图表或公式可用。"

    prompt = f'''你是专业学术PPT生成助手。请仔细阅读以下论文内容，完成两个任务。

## 任务一：文献阅读矩阵
请用中文生成该论文的阅读理解矩阵，包括：
- 核心问题：论文想解决什么问题？
- 研究空白：之前的工作为什么不够？
- 方法：提出了什么方法/框架？
- 核心证据：最关键的实验结果是什么？
- 局限性：论文的主要不足
- 导师可能提问：列出3个尖锐问题

## 任务二：PPT 提纲方案 + 图表/公式映射
基于阅读理解，设计一份组会汇报 PPT 的逐页方案。要求：
1. 每页标题简洁专业，内容精炼（要点不超过5条）
2. 按"研究背景→研究方法→实验与结果→总结展望"的逻辑组织
3. 为每页生成 speaker_notes（口头讲稿，200字以内）
4. 每条讲稿必须附带 orig 字段，标注该段讲稿依据的原文句子（原文照抄）
5. 内容要体现深度理解，而不是原文截断句子的堆砌

### 图表与公式映射规则
{assets_desc}

**严格要求：**
- 只能引用上面列出的编号，禁止编造不存在的图表或公式编号
- 如果展示图表，visual_assets 填写 {{"type": "figure", "index": 编号}}
- 如果展示公式，visual_assets 填写 {{"type": "equation", "index": 编号}}
- 每页最多引用1个图表或公式
- 公式必须放在"研究方法"相关页面，图表必须放在"实验与结果"相关页面
- **必须尽可能多地引用公式和图表**，不要遗漏！
- {allowed_refs}

### 公式解读要求（非常重要！每条都必须遵守！）
当某页引用了公式时，content 中必须包含对公式的**实质性中文解读**：
- 用"- 公式解读：..."的格式写在要点中
- 必须说明：公式中各核心符号的含义、公式的数学/经济/物理意义、在论文中起到什么作用
- speaker_notes 中也要用口语化方式解释这个公式
- 绝不允许写"（见讲稿）"这种空话！必须在 content 和 speaker_notes 中都写出具体解读内容
- 正确示例："公式解读：该方程是两阶段最小二乘法的核心方程，Y_it为个体i在t时期的健康结果，Insurance_it为是否获得医保，Z_i为彩票中选的工具变量，δ_1为局部平均处理效应(LATE)"

## 输出格式（严格 JSON）
```json
{{
  "reading_matrix": {{
    "core_problem": "核心问题",
    "research_gap": "研究空白",
    "method": "方法概述",
    "key_evidence": "核心证据",
    "limitations": "局限性",
    "advisor_questions": ["问题1", "问题2", "问题3"]
  }},
  "slides": [
    {{
      "title": "标题",
      "content": "- 要点1\\n- 要点2\\n- 要点3",
      "visual_assets": [],
      "speaker_notes": "这页讲的是...",
      "orig": "论文中支撑这页内容的原文关键句..."
    }}
  ]
}}
```

论文内容：
{text_with_pages}'''

    headers = {
        "Authorization": f"Bearer {DOUBAO_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7
    }

    print(f"[AI] 正在调用AI生成提纲... 文本长度: {len(text_with_pages)}")

    try:
        resp = requests.post(DOUBAO_ENDPOINT, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        reply = data["choices"][0]["message"]["content"]
        reply_json = json.loads(extract_json_from_reply(reply))

        # --- 过滤 AI 幻觉：移除引用了不存在资源的 visual_assets ---
        valid_figure_indices = {f["index"] for f in figures}
        valid_equation_indices = {e["index"] for e in equations}

        for slide in reply_json.get("slides", []):
            va = slide.get("visual_assets", [])
            filtered = []
            for v in va:
                v_type = v.get("type", "")
                v_index = v.get("index", 0)
                if v_type == "figure" and v_index in valid_figure_indices:
                    filtered.append(v)
                elif v_type == "equation" and v_index in valid_equation_indices:
                    filtered.append(v)
                else:
                    print(f"[AI] 过滤幻觉引用: type={v_type}, index={v_index}")
            slide["visual_assets"] = filtered

        print(f"[AI] 提纲生成成功，共 {len(reply_json.get('slides', []))} 页")
        return reply_json
    except Exception as e:
        print(f"[AI] 提纲生成失败: {e}")
        import traceback
        traceback.print_exc()
        return {"reading_matrix": {}, "slides": []}


# ====================== 第三步：PPT 生成 ======================
def generate_ppt(slides_data, extracted_data, template_path="template.pptx", output_path="output.pptx"):
    """根据提纲方案生成可编辑的 PPT，支持图表/公式图片插入。"""

    # ──────────────── 专业配色方案 ────────────────
    C_PRIMARY    = RGBColor(0x1B, 0x3A, 0x5C)   # 深藏青（主色）
    C_SECONDARY  = RGBColor(0x2E, 0x86, 0xC1)   # 中蓝（辅色）
    C_ACCENT     = RGBColor(0xE8, 0x7D, 0x2F)   # 暖橙（点缀）
    C_DARK       = RGBColor(0x0D, 0x1B, 0x2A)   # 近黑（封面/章节背景）
    C_TEXT       = RGBColor(0x2C, 0x3E, 0x50)   # 深灰（正文）
    C_LIGHT_TEXT = RGBColor(0xEC, 0xF0, 0xF1)   # 浅白（深色背景文字）
    C_LIGHT_GRAY = RGBColor(0xBD, 0xC3, 0xC7)   # 浅灰（辅助线）
    C_WHITE      = RGBColor(0xFF, 0xFF, 0xFF)

    # ──────────────── 构建 asset 查找表 ────────────────
    asset_map = {}
    for asset in extracted_data.get("visual_assets", []):
        key = (asset["type"], asset["index"])
        asset_map[key] = asset["path"]

    # ──────────────── 加载模板（仅取尺寸） ────────────────
    try:
        tmp = Presentation(template_path)
        slide_width = tmp.slide_width
        slide_height = tmp.slide_height
        print(f"[PPT] 模板尺寸: {slide_width/914400:.1f}x{slide_height/914400:.1f} 英寸")
    except Exception:
        slide_width = Inches(13.333)
        slide_height = Inches(7.5)
        print("[PPT] 模板加载失败，使用默认 16:9 尺寸")

    # 创建空白演示文稿（不使用模板版式，全部自行绘制）
    prs = Presentation()
    prs.slide_width = slide_width
    prs.slide_height = slide_height
    blank_layout = prs.slide_layouts[6]  # Blank layout

    sw_inch = slide_width / 914400
    sh_inch = slide_height / 914400

    section_titles = {"研究背景", "研究方法", "实验与结果", "总结展望",
                      "背景与动机", "方法", "结果", "讨论", "结论"}

    # ──────────────── 辅助函数 ────────────────
    def add_shape(slide, shape_type, left, top, width, height, fill_color=None, line_color=None, line_width=None):
        """添加形状到幻灯片"""
        shape = slide.shapes.add_shape(shape_type, left, top, width, height)
        shape.shadow.inherit = False
        if fill_color:
            shape.fill.solid()
            shape.fill.fore_color.rgb = fill_color
        else:
            shape.fill.background()
        if line_color:
            shape.line.color.rgb = line_color
            if line_width:
                shape.line.width = line_width
        else:
            shape.line.fill.background()
        return shape

    def add_textbox(slide, left, top, width, height, text, font_size=18,
                    font_color=C_TEXT, bold=False, alignment=PP_ALIGN.LEFT,
                    font_name="微软雅黑", anchor=MSO_ANCHOR.TOP):
        """添加文本框到幻灯片"""
        txBox = slide.shapes.add_textbox(left, top, width, height)
        tf = txBox.text_frame
        tf.word_wrap = True
        tf.auto_size = None
        p = tf.paragraphs[0]
        p.text = text
        p.font.size = Pt(font_size)
        p.font.color.rgb = font_color
        p.font.bold = bold
        p.font.name = font_name
        p.alignment = alignment
        try:
            tf.paragraphs[0].space_before = Pt(0)
            tf.paragraphs[0].space_after = Pt(0)
        except:
            pass
        return txBox

    def make_cover_slide(title_text, subtitle=""):
        """封面页：深色背景 + 大标题 + 橙色装饰线"""
        slide = prs.slides.add_slide(blank_layout)

        # 深色全屏背景
        add_shape(slide, MSO_SHAPE.RECTANGLE, 0, 0, slide_width, slide_height, fill_color=C_DARK)

        # 左侧橙色竖条装饰
        add_shape(slide, MSO_SHAPE.RECTANGLE, Inches(0.8), Inches(2.0), Inches(0.08), Inches(2.8), fill_color=C_ACCENT)

        # 主标题
        add_textbox(slide, Inches(1.2), Inches(2.0), Inches(sw_inch - 2.0), Inches(2.0),
                    title_text, font_size=40, font_color=C_WHITE, bold=True,
                    alignment=PP_ALIGN.LEFT)

        # 副标题/论文信息
        if subtitle:
            add_textbox(slide, Inches(1.2), Inches(4.2), Inches(sw_inch - 2.0), Inches(0.6),
                        subtitle, font_size=16, font_color=C_LIGHT_GRAY, bold=False,
                        alignment=PP_ALIGN.LEFT)

        # 底部蓝色细线
        add_shape(slide, MSO_SHAPE.RECTANGLE, Inches(1.2), Inches(5.4), Inches(3.0), Inches(0.03), fill_color=C_SECONDARY)

        return slide

    def make_section_slide(title_text):
        """章节分隔页：藏青背景 + 白色大标题 + 橙色横线"""
        slide = prs.slides.add_slide(blank_layout)

        # 藏青色全屏背景
        add_shape(slide, MSO_SHAPE.RECTANGLE, 0, 0, slide_width, slide_height, fill_color=C_PRIMARY)

        # 左侧橙色竖条
        add_shape(slide, MSO_SHAPE.RECTANGLE, Inches(1.0), Inches(2.6), Inches(0.06), Inches(2.2), fill_color=C_ACCENT)

        # 章节标题
        add_textbox(slide, Inches(1.4), Inches(2.8), Inches(sw_inch - 2.4), Inches(1.6),
                    title_text, font_size=36, font_color=C_WHITE, bold=True,
                    alignment=PP_ALIGN.LEFT)

        # 底部细线
        add_shape(slide, MSO_SHAPE.RECTANGLE, Inches(1.4), Inches(4.6), Inches(2.5), Inches(0.03), fill_color=C_ACCENT)

        return slide

    def make_content_slide(title_text, content_text, visual_assets=None):
        """内容页：白色背景 + 顶部色条 + 标题 + 内容 + 图片"""
        slide = prs.slides.add_slide(blank_layout)
        visual_assets = visual_assets or []
        has_visual = bool(visual_assets)

        # 白色背景
        add_shape(slide, MSO_SHAPE.RECTANGLE, 0, 0, slide_width, slide_height, fill_color=C_WHITE)

        # 顶部藏青色条
        add_shape(slide, MSO_SHAPE.RECTANGLE, 0, 0, slide_width, Inches(0.06), fill_color=C_PRIMARY)

        # 左侧窄竖条（点缀）
        add_shape(slide, MSO_SHAPE.RECTANGLE, 0, 0, Inches(0.06), slide_height, fill_color=C_SECONDARY)

        # 标题区域背景
        add_shape(slide, MSO_SHAPE.RECTANGLE, Inches(0.06), Inches(0.06), Inches(sw_inch - 0.06), Inches(1.0), fill_color=RGBColor(0xF0, 0xF4, 0xF8))

        # 标题文字
        add_textbox(slide, Inches(0.6), Inches(0.15), Inches(sw_inch - 1.2), Inches(0.8),
                    title_text, font_size=26, font_color=C_PRIMARY, bold=True,
                    alignment=PP_ALIGN.LEFT)

        # 标题下方橙色短线
        add_shape(slide, MSO_SHAPE.RECTANGLE, Inches(0.6), Inches(1.06), Inches(1.2), Inches(0.04), fill_color=C_ACCENT)

        # --- 内容区域 ---
        content_left = Inches(0.6)
        content_top = Inches(1.3)
        if has_visual:
            content_width = Inches(sw_inch * 0.52)
        else:
            content_width = Inches(sw_inch - 1.5)
        content_height = Inches(sh_inch - 2.0)

        txBox = slide.shapes.add_textbox(content_left, content_top, content_width, content_height)
        tf = txBox.text_frame
        tf.word_wrap = True
        tf.auto_size = None

        lines = content_text.split("\n")
        first = True
        for line in lines:
            line = line.strip().lstrip("- ").strip()
            if not line:
                continue
            if first:
                p = tf.paragraphs[0]
                first = False
            else:
                p = tf.add_paragraph()

            # 判断是否是子标题行（•、●、数字编号）
            is_subhead = line.startswith(("•", "●")) or (len(line) > 2 and line[0].isdigit() and line[1] in ".、")

            if is_subhead:
                # 子标题：蓝灰色 + 粗体 + 前缀圆点
                clean = line.lstrip("•●").strip()
                p.text = "▸ " + clean
                p.font.size = Pt(17)
                p.font.color.rgb = C_PRIMARY
                p.font.bold = True
                p.font.name = "微软雅黑"
                p.space_before = Pt(10)
                p.space_after = Pt(4)
            else:
                # 普通正文
                p.text = line
                p.font.size = Pt(15)
                p.font.color.rgb = C_TEXT
                p.font.bold = False
                p.font.name = "微软雅黑"
                p.space_before = Pt(3)
                p.space_after = Pt(3)

        # --- 底部信息栏 ---
        add_shape(slide, MSO_SHAPE.RECTANGLE, 0, Inches(sh_inch - 0.35), slide_width, Inches(0.35), fill_color=C_PRIMARY)
        add_textbox(slide, Inches(0.5), Inches(sh_inch - 0.32), Inches(3), Inches(0.28),
                    "Pre-Assistant", font_size=9, font_color=C_LIGHT_GRAY, bold=False)

        return slide

    # ──────────────── 预处理：将 AI 未引用的公式/图表自动分配到对应章节幻灯片 ────────────────
    referenced_keys = set()
    for slide_info in slides_data:
        for va in slide_info.get("visual_assets", []):
            referenced_keys.add((va.get("type", ""), va.get("index", 0)))

    unreferenced = [a for a in extracted_data.get("visual_assets", [])
                    if (a["type"], a["index"]) not in referenced_keys]

    if unreferenced:
        unreferenced_eqs = [a for a in unreferenced if a["type"] == "equation"]
        unreferenced_figs = [a for a in unreferenced if a["type"] == "figure"]

        method_indices = []
        result_indices = []
        other_content_indices = []

        for i, s in enumerate(slides_data):
            title = s.get("title", "")
            if any(kw in title for kw in ["方法", "模型", "框架", "Method"]):
                method_indices.append(i)
            elif any(kw in title for kw in ["结果", "实验", "实证", "Result", "Experiment"]):
                result_indices.append(i)
            elif title not in section_titles:
                other_content_indices.append(i)

        eq_queue = list(unreferenced_eqs)
        assign_targets = method_indices + other_content_indices + result_indices
        for i in assign_targets:
            if not eq_queue:
                break
            eq = eq_queue.pop(0)
            slides_data[i].setdefault("visual_assets", []).append(
                {"type": "equation", "index": eq["index"]}
            )
            content = slides_data[i].get("content", "")
            preview = eq.get("text_preview", "")
            if preview and "(图片公式" not in preview:
                content += f"\n- 公式解读：{preview}"
            else:
                content += f"\n- 公式来源：论文第{eq['page']}页"
            slides_data[i]["content"] = content
            notes = slides_data[i].get("speaker_notes", "")
            notes += f" 此外，这页还展示了论文第{eq['page']}页的关键公式。"
            slides_data[i]["speaker_notes"] = notes
            print(f"[PPT] 公式{eq['index']}分配到: {slides_data[i]['title']}")

        fig_queue = list(unreferenced_figs)
        for i in result_indices + other_content_indices + method_indices:
            if not fig_queue:
                break
            fig = fig_queue.pop(0)
            slides_data[i].setdefault("visual_assets", []).append(
                {"type": "figure", "index": fig["index"]}
            )
            notes = slides_data[i].get("speaker_notes", "")
            notes += f" 这页包含了论文第{fig['page']}页的图表。"
            slides_data[i]["speaker_notes"] = notes
            print(f"[PPT] 图{fig['index']}分配到: {slides_data[i]['title']}")

        for eq in eq_queue:
            insert_pos = method_indices[-1] + 1 if method_indices else len(slides_data)
            preview = eq.get("text_preview", "见论文原文")
            new_slide = {
                "title": f"关键公式（第{eq['page']}页）",
                "content": f"- 公式含义：{preview}",
                "visual_assets": [{"type": "equation", "index": eq["index"]}],
                "speaker_notes": f"这页展示论文第{eq['page']}页的关键公式。{preview}",
                "orig": ""
            }
            slides_data.insert(insert_pos, new_slide)
            method_indices = [j + 1 if j >= insert_pos else j for j in method_indices]
            result_indices = [j + 1 if j >= insert_pos else j for j in result_indices]
            other_content_indices = [j + 1 if j >= insert_pos else j for j in other_content_indices]
            print(f"[PPT] 公式{eq['index']}作为独立页插入位置{insert_pos}")

    # ──────────────── 逐页生成 ────────────────
    RENDER_DPI = 200

    for idx, slide_info in enumerate(slides_data):
        title_text = slide_info.get("title", "无标题")
        content_text = slide_info.get("content", "")
        visual_assets = slide_info.get("visual_assets", [])
        is_section = title_text in section_titles

        # 封面页
        if idx == 0:
            slide = make_cover_slide(title_text)
            # speaker notes
            notes = slide_info.get("speaker_notes", "")
            if notes:
                slide.notes_slide.notes_text_frame.text = notes
            print(f"[PPT] 已生成封面: {title_text}")
            continue

        # 章节分隔页
        if is_section:
            slide = make_section_slide(title_text)
            notes = slide_info.get("speaker_notes", "")
            if notes:
                slide.notes_slide.notes_text_frame.text = notes
            print(f"[PPT] 已生成章节页: {title_text}")
            continue

        # 内容页
        slide = make_content_slide(title_text, content_text, visual_assets)

        # --- 插入图表/公式图片 ---
        for va in visual_assets:
            va_type = va.get("type", "")
            va_index = va.get("index", 0)
            key = (va_type, va_index)
            img_path = asset_map.get(key)

            if not img_path or not os.path.exists(img_path):
                print(f"[PPT] 图片未找到，跳过: {key}")
                continue

            try:
                pil_img = Image.open(img_path)
                img_w_px, img_h_px = pil_img.size
                img_w_inch = img_w_px / RENDER_DPI
                img_h_inch = img_h_px / RENDER_DPI

                if va_type == "equation":
                    # 公式：居中放在内容区域下方
                    max_w_inch = sw_inch * 0.70
                    max_h_inch = sh_inch * 0.28
                    scale = min(max_w_inch / img_w_inch, max_h_inch / img_h_inch, 3.0)
                    final_w = Inches(img_w_inch * scale)
                    final_h = Inches(img_h_inch * scale)
                    img_left = int((slide_width - int(final_w)) / 2)
                    img_top = Inches(sh_inch * 0.55)
                else:
                    # 图表：右侧
                    max_w_inch = sw_inch * 0.38
                    max_h_inch = sh_inch * 0.50
                    scale = min(max_w_inch / img_w_inch, max_h_inch / img_h_inch, 1.5)
                    final_w = Inches(img_w_inch * scale)
                    final_h = Inches(img_h_inch * scale)
                    img_left = Inches(sw_inch * 0.58)
                    img_top = Inches(sh_inch * 0.25)

                slide.shapes.add_picture(img_path, img_left, img_top, final_w, final_h)
                print(f"[PPT] 已插入: {va_type}{va_index} ({img_w_inch*scale:.1f}x{img_h_inch*scale:.2f} in)")
            except Exception as e:
                print(f"[PPT] 图片插入失败: {e}")

        # speaker notes
        notes = slide_info.get("speaker_notes", "")
        if notes:
            slide.notes_slide.notes_text_frame.text = notes

        print(f"[PPT] 已生成: {title_text}")

    prs.save(output_path)
    print(f"[PPT] 保存完成: {output_path}")


# ====================== 第三步（B）：SVG 精美模式 ======================

# 设计风格预设
DESIGN_STYLES = {
    "academic": {
        "name": "学术蓝",
        "background": "#FFFFFF",
        "secondary_bg": "#F0F4F8",
        "primary": "#1B3A5C",
        "accent": "#2E86C1",
        "secondary_accent": "#E87D2F",
        "body_text": "#2C3E50",
        "secondary_text": "#5D6D7E",
        "border": "#BDC3C7",
    },
    "consulting": {
        "name": "咨询深蓝",
        "background": "#FFFFFF",
        "secondary_bg": "#EBF5FB",
        "primary": "#0C2340",
        "accent": "#1A73E8",
        "secondary_accent": "#F4B400",
        "body_text": "#202124",
        "secondary_text": "#5F6368",
        "border": "#DADCE0",
    },
    "tech": {
        "name": "科技暗黑",
        "background": "#1A1A2E",
        "secondary_bg": "#16213E",
        "primary": "#0F3460",
        "accent": "#00D2FF",
        "secondary_accent": "#E94560",
        "body_text": "#E0E0E0",
        "secondary_text": "#A0A0A0",
        "border": "#333366",
    },
    "nature": {
        "name": "自然绿",
        "background": "#FFFFFF",
        "secondary_bg": "#F0FFF0",
        "primary": "#2E7D32",
        "accent": "#43A047",
        "secondary_accent": "#FF8F00",
        "body_text": "#1B5E20",
        "secondary_text": "#558B2F",
        "border": "#A5D6A7",
    },
}


def _call_llm(prompt, temperature=0.7, max_tokens=16384):
    """调用 LLM API，返回文本回复。"""
    headers = {
        "Authorization": f"Bearer {DOUBAO_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    resp = requests.post(DOUBAO_ENDPOINT, headers=headers, json=payload, timeout=180)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _build_svg_prompt(slide_info, page_num, total_pages, style, image_hrefs=None):
    """构建单页 SVG 生成 prompt（借鉴 paper-ppt-agent 的设计规范）。"""
    image_hrefs = image_hrefs or []
    title = slide_info.get("title", "")
    content = slide_info.get("content", "")
    section_titles = {"研究背景", "研究方法", "实验与结果", "总结展望",
                      "背景与动机", "方法", "结果", "讨论", "结论"}
    is_cover = (page_num == 1)
    is_section = title in section_titles

    # 根据页面类型生成布局指导
    if is_cover:
        layout_guide = f"""COVER PAGE layout:
- Full dark background using linearGradient from "{style['primary']}" to a darker shade
- Large bold title (font-size 48-56px), fill="#FFFFFF", positioned center-left (x=80-120)
- LEFT accent bar: vertical rect (x=60, width=6px, height~200px), fill="{style['secondary_accent']}"
- Below title: horizontal gradient overlay bar (width~300px, height=3px)
- Bottom: subtle footer area with brand text (font-size=10px), fill=white with low opacity
- Optional: 2-3 decorative circles/ovals with very low fill-opacity (0.05-0.08) in the right area"""
    elif is_section:
        layout_guide = f"""SECTION DIVIDER PAGE layout:
- Full dark background: fill="{style['primary']}"
- LEFT accent bar: vertical rect (x=60, width=6px, height~180px), fill="{style['secondary_accent']}"
- Section title: large bold text (font-size 40-48px), fill="#FFFFFF", x=100
- Below title: horizontal accent line (width~250px, height=3px), fill="{style['accent']}"
- Decorative: 1-2 circles with fill="{style['accent']}" fill-opacity="0.06" on the right side
- Bottom footer bar: rect (full width, height=30px), fill=darker shade of primary"""
    else:
        has_images = "YES" if image_hrefs else "NO"
        img_layout = """- If images exist: place them on the RIGHT side (x > 700), with a rounded-rect border (rx=8, stroke=border color, no fill)
- Image container: ~400x300px area on right, image centered within""" if image_hrefs else ""
        layout_guide = f"""CONTENT PAGE layout (images: {has_images}):
- White background: fill="{style['background']}"
- TOP accent bar: horizontal rect (full width, height=5px), fill="{style['primary']}"
- LEFT accent bar: vertical rect (width=4px, full height), fill="{style['accent']}"
- Title background band: rounded rect (rx=0, fill="{style['secondary_bg']}", height=80px)
- Title: bold text (font-size 28-32px), fill="{style['primary']}", inside band
- Below title band: small accent rect (width=80px, height=3px), fill="{style['secondary_accent']}"
- Content area starts at y~130, left margin x=80
- Bullet points: separate <text> elements, ~34px apart
  - Sub-heading items: prefix "▸ ", font-weight="bold", fill="{style['primary']}", font-size=18
  - Body text: fill="{style['body_text']}", font-size=17
  - Wrap long text properly — do NOT overlap text elements
- Use <g> groups to wrap logically related elements (card = background rect + text)
- BOTTOM footer: rect (full width, height=28px, y=692), fill="{style['primary']}"
- Footer text: "Pre-Assistant", font-size=10, fill="#AAAAAA", x=30, y=710
{img_layout}"""

    img_section = ""
    if image_hrefs:
        img_section = "\n## Paper Figure Guidance\nUse these image references in <image> elements:\n"
        for i, href in enumerate(image_hrefs):
            img_section += f'- <image href="{href}" x="LEFT" y="TOP" width="WIDTH" height="HEIGHT" preserveAspectRatio="xMidYMid slice"/>\n'
        img_section += "Place paper figures on the RIGHT side of the slide. Use clipPath with a rounded rect for a polished look.\n"

    return f"""You are an expert SVG page generator for academic presentations. Generate a complete SVG file for page {page_num}/{total_pages}.

{layout_guide}

## Color Palette (use EXACTLY these hex values — no others)
- background: {style['background']}
- secondary_bg: {style['secondary_bg']}
- primary: {style['primary']}
- accent: {style['accent']}
- secondary_accent: {style['secondary_accent']}
- body_text: {style['body_text']}
- secondary_text: {style['secondary_text']}
- border: {style['border']}

## Page Content
- Title: {title}
- Content: {content}
{img_section}

## SVG Technical Standards

### Canvas
```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" width="1280" height="720">
```

### BANNED Features (will cause rendering failure)
- `<mask>`, `<style>`, `class` attributes, external CSS
- `<foreignObject>`, `<symbol>` + `<use>`
- `textPath`, `@font-face`, `<animate*>`, `<script>`, `<iframe>`
- `rgba()` — use `fill-opacity` / `stroke-opacity` instead
- `<g opacity="...">` — set opacity per-element instead
- Nesting `<text>` inside `<text>` — use `<tspan>` for inline styling

### PPT Compatibility
| Banned | Use Instead |
|--------|-------------|
| `fill="rgba(255,255,255,0.1)"` | `fill="#FFFFFF" fill-opacity="0.1"` |
| `<g opacity="0.2">...</g>` | Set `fill-opacity` / `stroke-opacity` on each child |

### Element Grouping (Mandatory)
Wrap logically related elements in `<g>` tags (produces PowerPoint groups):
- Card/panel = background rect + shadow + text content
- List item = bullet + text
- Page header = title + subtitle + accent decoration
- Page footer = page number + branding

### Shadow Technique (use for cards/panels)
```xml
<defs>
  <filter id="softShadow" x="-15%" y="-15%" width="140%" height="140%">
    <feGaussianBlur in="SourceAlpha" stdDeviation="12"/>
    <feOffset dx="0" dy="6" result="offsetBlur"/>
    <feFlood flood-color="#000000" flood-opacity="0.15" result="shadowColor"/>
    <feComposite in="shadowColor" in2="offsetBlur" operator="in" result="shadow"/>
    <feMerge><feMergeNode in="shadow"/><feMergeNode in="SourceGraphic"/></feMerge>
  </filter>
</defs>
<rect x="60" y="60" width="400" height="240" rx="12" fill="#FFFFFF" filter="url(#softShadow)"/>
```

### Gradient Overlays (for cover/section pages)
```xml
<defs>
  <linearGradient id="coverGrad" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0%" stop-color="{style['primary']}"/>
    <stop offset="100%" stop-color="{style['primary']}" fill-opacity="0.7"/>
  </linearGradient>
</defs>
<rect width="1280" height="720" fill="url(#coverGrad)"/>
```

### Text Rules
- font-family="Microsoft YaHei, sans-serif" for ALL text (Chinese support)
- y on <text> is baseline position (add font_size*0.35 for visual centering)
- All colors as hex (#RRGGBB) — no named colors, no rgb()
- For KPI/metric rows: number and label must share the same y baseline
- Never place multiple <text> elements at the same x/y position
- All text must be XML-safe: &amp; for &, &lt; for <, &gt; for >, &apos; for '
- Ensure sufficient contrast: dark text on light backgrounds, light text on dark backgrounds
- Font sizes: cover title 48-56px, section title 40-48px, page title 28-32px, body 17-18px, caption 14px

### Quality Checklist
- No text overlap (check all text element positions)
- No text out of canvas bounds (0-1280 x 0-720)
- Consistent margins (minimum 60px from edges)
- Every design decision serves communication, not decoration

Output ONLY the raw SVG code starting with <?xml version="1.0" encoding="UTF-8"?>. No markdown, no explanation."""


def generate_ppt_svg_mode(slides_data, extracted_data, style_key="academic", output_path="output.pptx"):
    """SVG 精美模式：用精心设计的 SVG 模板程序化生成，再转换为原生 DrawingML 形状。"""

    if not RESVG_AVAILABLE:
        raise RuntimeError("resvg-py 未安装，无法使用精美模式。请运行: pip install resvg-py")

    from svg_to_pptx import create_pptx as svg_create_pptx

    style = DESIGN_STYLES.get(style_key, DESIGN_STYLES["academic"])
    print(f"[SVG] 精美模式启动，风格: {style['name']}")

    # ──── 构建 asset 查找表 ────
    asset_map = {}
    for asset in extracted_data.get("visual_assets", []):
        key = (asset["type"], asset["index"])
        asset_map[key] = asset["path"]

    total = len(slides_data)
    print(f"[SVG] 共 {total} 页，生成 SVG...")

    # ──── 用模板程序化生成 SVG（不依赖 LLM） ────
    import tempfile
    svg_tmp_dir = tempfile.mkdtemp(prefix="svg_slides_")
    svg_file_paths = []
    notes_dict = {}

    for idx, slide_info in enumerate(slides_data):
        page_num = idx + 1
        title = slide_info.get("title", "")
        content = slide_info.get("content", "")
        is_cover = (page_num == 1)
        section_titles = {"研究背景", "研究方法", "实验与结果", "总结展望",
                          "背景与动机", "方法", "结果", "讨论", "结论"}
        is_section = title in section_titles

        # 收集该页的图片，区分图表和公式
        slide_figures = []
        slide_equations = []
        for va in slide_info.get("visual_assets", []):
            va_type = va.get("type", "")
            va_index = va.get("index", 0)
            key = (va_type, va_index)
            img_path = asset_map.get(key)
            if img_path and os.path.exists(img_path):
                if va_type == "equation":
                    slide_equations.append(img_path)
                else:
                    slide_figures.append(img_path)

        if is_cover:
            svg_code = _svg_cover(title, slide_info.get("subtitle", ""), style)
        elif is_section:
            svg_code = _svg_section(title, style)
        else:
            svg_code = _svg_content(title, content, style, slide_figures, slide_equations)

        # 清理 & 保存
        svg_clean = _sanitize_svg(svg_code)
        svg_filename = f"slide_{page_num:03d}.svg"
        svg_path = os.path.join(svg_tmp_dir, svg_filename)
        with open(svg_path, "w", encoding="utf-8") as f:
            f.write(svg_clean)
        svg_file_paths.append(Path(svg_path))

        # speaker notes
        stem = Path(svg_path).stem
        notes_text = slide_info.get("speaker_notes", "")
        if notes_text:
            notes_dict[stem] = notes_text

        print(f"[SVG]   第{page_num}页 SVG 生成完成")

    # ──── 转换为原生 DrawingML PPTX ────
    print("[SVG] 转换 SVG → 原生 PPTX (DrawingML)...")
    try:
        result_path = svg_create_pptx(
            svg_files=svg_file_paths,
            output_path=Path(output_path),
            notes=notes_dict,
            width_px=1280,
            height_px=720,
        )
        print(f"[SVG] ✓ 原生 PPTX 保存完成: {output_path}")
    except Exception as e:
        print(f"[SVG] 原生转换失败，回退到 PNG 模式: {e}")
        # 回退：重新生成 SVG 列表用于 PNG
        svg_results = []
        for idx, slide_info in enumerate(slides_data):
            page_num = idx + 1
            title = slide_info.get("title", "")
            content = slide_info.get("content", "")
            is_cover = (page_num == 1)
            section_titles = {"研究背景", "研究方法", "实验与结果", "总结展望",
                              "背景与动机", "方法", "结果", "讨论", "结论"}
            is_section = title in section_titles
            slide_figures = []
            slide_equations = []
            for va in slide_info.get("visual_assets", []):
                va_type = va.get("type", "")
                va_index = va.get("index", 0)
                key = (va_type, va_index)
                img_path = asset_map.get(key)
                if img_path and os.path.exists(img_path):
                    if va_type == "equation":
                        slide_equations.append(img_path)
                    else:
                        slide_figures.append(img_path)

            if is_cover:
                svg_results.append(_svg_cover(title, slide_info.get("subtitle", ""), style))
            elif is_section:
                svg_results.append(_svg_section(title, style))
            else:
                svg_results.append(_svg_content(title, content, style, slide_figures, slide_equations))

        _generate_ppt_png_fallback(svg_results, slides_data, style, output_path)
    finally:
        import shutil
        shutil.rmtree(svg_tmp_dir, ignore_errors=True)


def _sanitize_svg(svg_code):
    """清理 LLM 生成的 SVG，修复常见问题。"""
    # 修复未转义的 & (不在已有实体引用中)
    svg_code = re.sub(r'&(?!(?:amp|lt|gt|quot|apos|#[0-9]+);)', '&amp;', svg_code)
    # 移除可能导致问题的 <?xml ?> 声明（resvg 不需要）
    # 保留它也没问题，但确保格式正确
    return svg_code


# ====================== SVG 模板生成（程序化，不依赖 LLM） ======================

def _svg_cover(title, subtitle, style):
    """生成封面页 SVG — 精美深色渐变背景 + 装饰几何元素。"""
    s = style
    # 深色渐变背景
    svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 1280 720" width="1280" height="720">
  <defs>
    <linearGradient id="bgGrad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="{s['primary']}"/>
      <stop offset="100%" stop-color="#0D1B2A"/>
    </linearGradient>
    <linearGradient id="accentLine" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="{s['secondary_accent']}"/>
      <stop offset="100%" stop-color="{s['accent']}"/>
    </linearGradient>
  </defs>

  <!-- 背景 -->
  <rect width="1280" height="720" fill="url(#bgGrad)"/>

  <!-- 右侧装饰圆 -->
  <circle cx="1050" cy="180" r="200" fill="{s['accent']}" fill-opacity="0.04"/>
  <circle cx="1150" cy="400" r="140" fill="{s['secondary_accent']}" fill-opacity="0.03"/>
  <circle cx="900" cy="550" r="100" fill="{s['accent']}" fill-opacity="0.05"/>

  <!-- 右侧装饰线条 -->
  <line x1="950" y1="0" x2="950" y2="720" stroke="{s['accent']}" stroke-opacity="0.08" stroke-width="1"/>
  <line x1="1050" y1="0" x2="1050" y2="720" stroke="{s['accent']}" stroke-opacity="0.05" stroke-width="1"/>

  <!-- 左侧强调竖条 -->
  <rect x="70" y="240" width="6" height="240" fill="{s['secondary_accent']}"/>

  <!-- 标题 -->
  <text x="100" y="360" font-size="48" fill="#FFFFFF" font-family="Microsoft YaHei, sans-serif" font-weight="bold">{_xml_escape(title)}</text>

  <!-- 渐变装饰线 -->
  <rect x="100" y="385" width="300" height="4" rx="2" fill="url(#accentLine)"/>

  <!-- 副标题 -->
  <text x="100" y="430" font-size="20" fill="#FFFFFF" fill-opacity="0.7" font-family="Microsoft YaHei, sans-serif">{_xml_escape(subtitle) if subtitle else "Academic Presentation"}</text>

  <!-- 底部品牌 -->
  <rect x="0" y="685" width="1280" height="35" fill="#000000" fill-opacity="0.2"/>
  <text x="50" y="708" font-size="12" fill="#FFFFFF" fill-opacity="0.5" font-family="Microsoft YaHei, sans-serif">Pre-Assistant</text>
</svg>'''
    return svg


def _svg_section(title, style):
    """生成章节分隔页 SVG — 深色背景 + 大标题 + 装饰元素。"""
    s = style
    svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 1280 720" width="1280" height="720">
  <defs>
    <linearGradient id="secGrad" x1="0" y1="0" x2="1" y2="0.5">
      <stop offset="0%" stop-color="{s['primary']}"/>
      <stop offset="100%" stop-color="#0D1B2A"/>
    </linearGradient>
    <linearGradient id="secAccent" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="{s['secondary_accent']}"/>
      <stop offset="100%" stop-color="{s['accent']}"/>
    </linearGradient>
  </defs>

  <!-- 背景 -->
  <rect width="1280" height="720" fill="url(#secGrad)"/>

  <!-- 装饰几何 -->
  <circle cx="1050" cy="350" r="180" fill="{s['accent']}" fill-opacity="0.04"/>
  <circle cx="1150" cy="550" r="120" fill="{s['secondary_accent']}" fill-opacity="0.03"/>
  <rect x="1100" y="100" width="80" height="80" fill="{s['accent']}" fill-opacity="0.04" transform="rotate(45 1140 140)"/>

  <!-- 左侧强调竖条 -->
  <rect x="80" y="260" width="6" height="200" fill="{s['secondary_accent']}"/>

  <!-- 章节标题 -->
  <text x="115" y="380" font-size="44" fill="#FFFFFF" font-family="Microsoft YaHei, sans-serif" font-weight="bold">{_xml_escape(title)}</text>

  <!-- 渐变装饰线 -->
  <rect x="115" y="400" width="250" height="4" rx="2" fill="url(#secAccent)"/>

  <!-- 底部 -->
  <rect x="0" y="685" width="1280" height="35" fill="#000000" fill-opacity="0.2"/>
  <text x="50" y="708" font-size="12" fill="#FFFFFF" fill-opacity="0.5" font-family="Microsoft YaHei, sans-serif">Pre-Assistant</text>
</svg>'''
    return svg


def _svg_content(title, content, style, images=None, equations=None):
    """生成内容页 SVG — 白色背景 + 卡片式布局 + 公式区 + 图片区域。"""
    s = style
    images = images or []
    equations = equations or []

    # 解析内容为行列表
    lines = []
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        # 识别层级
        is_subheading = False
        prefix = ""
        clean_line = line.lstrip("- ").lstrip("• ").lstrip("● ").strip()
        if clean_line and clean_line[0].isdigit() and len(clean_line) > 1 and clean_line[1] in ".、)":
            is_subheading = True
            clean_line = clean_line[2:].strip()
        elif line.strip().startswith(("•", "●")):
            prefix = "  "
        lines.append((clean_line, is_subheading, prefix))

    # 判断是否有图片来决定布局
    has_images = len(images) > 0
    content_width = 580 if has_images else 1140
    content_start_x = 70

    # ──── 构建内容区 SVG ────
    content_svg = ""
    y = 130
    card_items = []  # 收集卡片项目
    current_card = []

    # 将行分组为卡片（每个子标题开始一个新卡片）
    for line_text, is_subheading, prefix in lines:
        if is_subheading and current_card:
            card_items.append(current_card)
            current_card = []
        current_card.append((line_text, is_subheading))
    if current_card:
        card_items.append(current_card)

    if not card_items:
        # 无内容时放一个空卡片
        card_items = [["(无内容)", False]]

    # 绘制卡片
    card_y = 120
    for card_idx, card in enumerate(card_items):
        if card_y > 640:
            break  # 超出页面

        # 计算卡片高度
        card_line_count = len(card)
        card_height = max(80, card_line_count * 32 + 50)

        # 卡片背景（带阴影效果的叠层）
        content_svg += f'''  <rect x="{content_start_x}" y="{card_y + 3}" width="{content_width}" height="{card_height}" rx="10" fill="#000000" fill-opacity="0.06"/>
  <rect x="{content_start_x}" y="{card_y}" width="{content_width}" height="{card_height}" rx="10" fill="#FFFFFF" stroke="{s['border']}" stroke-width="0.5"/>
'''

        # 卡片左侧强调条
        content_svg += f'  <rect x="{content_start_x}" y="{card_y}" width="4" height="{card_height}" rx="2" fill="{s['accent']}"/>\n'

        # 卡片内容
        text_y = card_y + 35
        for line_text, is_subheading in card:
            if is_subheading:
                content_svg += f'  <text x="{content_start_x + 22}" y="{text_y}" font-size="18" fill="{s["primary"]}" font-family="Microsoft YaHei, sans-serif" font-weight="bold">{_xml_escape(line_text)}</text>\n'
                text_y += 32
            else:
                # 普通文本带小圆点
                content_svg += f'  <circle cx="{content_start_x + 28}" cy="{text_y - 5}" r="3" fill="{s["accent"]}"/>\n'
                content_svg += f'  <text x="{content_start_x + 40}" y="{text_y}" font-size="16" fill="{s["body_text"]}" font-family="Microsoft YaHei, sans-serif">{_xml_escape(line_text)}</text>\n'
                text_y += 30

        card_y += card_height + 15

    # ──── 构建公式区 SVG ────
    equation_svg = ""
    if equations:
        eq_y = card_y + 5 if card_y < 500 else 120
        if not has_images:
            # 无图片时公式居中显示
            for eq_idx, eq_path in enumerate(equations[:3]):  # 最多3个公式
                try:
                    with open(eq_path, "rb") as f:
                        eq_data = base64.b64encode(f.read()).decode("utf-8")
                    # 读取图片尺寸
                    from PIL import Image as PILImage
                    with PILImage.open(eq_path) as im:
                        eq_w, eq_h = im.size
                    # 缩放：最大宽度1000px，最大高度80px
                    scale = min(1000 / max(eq_w, 1), 80 / max(eq_h, 1), 1.0)
                    disp_w = int(eq_w * scale)
                    disp_h = int(eq_h * scale)
                    eq_x = (1280 - disp_w) // 2  # 居中

                    # 公式容器
                    equation_svg += f'''  <rect x="{eq_x - 15}" y="{eq_y}" width="{disp_w + 30}" height="{disp_h + 30}" rx="8" fill="{s['secondary_bg']}" stroke="{s['border']}" stroke-width="0.5"/>
  <image href="data:image/png;base64,{eq_data}" x="{eq_x}" y="{eq_y + 15}" width="{disp_w}" height="{disp_h}" preserveAspectRatio="xMidYMid meet"/>
  <text x="{eq_x + disp_w + 5}" y="{eq_y + disp_h + 10}" font-size="12" fill="{s['secondary_text']}" font-family="Microsoft YaHei, sans-serif">({eq_idx + 1})</text>
'''
                    eq_y += disp_h + 50
                except Exception as e:
                    print(f"[SVG]   公式 {eq_path} 编码失败: {e}")

    # ──── 构建图片区 SVG ────
    image_svg = ""
    if has_images:
        img_x = 700
        img_y = 120
        for img_idx, img_path in enumerate(images[:2]):  # 最多显示2张图
            # 读取图片为 base64
            try:
                with open(img_path, "rb") as f:
                    img_data = base64.b64encode(f.read()).decode("utf-8")
                ext = os.path.splitext(img_path)[1].lower().lstrip(".")
                mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif"}.get(ext, "image/png")
                data_uri = f"data:{mime};base64,{img_data}"

                # 图片容器（阴影 + 圆角边框）
                image_svg += f'''  <rect x="{img_x + 3}" y="{img_y + 3}" width="520" height="250" rx="10" fill="#000000" fill-opacity="0.06"/>
  <rect x="{img_x}" y="{img_y}" width="520" height="250" rx="10" fill="#FFFFFF" stroke="{s['border']}" stroke-width="0.5"/>
  <image href="{data_uri}" x="{img_x + 10}" y="{img_y + 10}" width="500" height="230" preserveAspectRatio="xMidYMid meet"/>
'''
                # 图片编号标签
                img_label = f"Fig.{img_idx + 1}" if "/figures/" in img_path.replace("\\", "/") else f"Fig.{img_idx + 1}"
                image_svg += f'  <rect x="{img_x + 10}" y="{img_y + 220}" width="60" height="22" rx="4" fill="{s["primary"]}" fill-opacity="0.8"/>\n'
                image_svg += f'  <text x="{img_x + 18}" y="{img_y + 235}" font-size="11" fill="#FFFFFF" font-family="Microsoft YaHei, sans-serif">{img_label}</text>\n'

                img_y += 275
            except Exception as e:
                print(f"[SVG]   图片 {img_path} 编码失败: {e}")

        # 如果有公式，放在图片下方
        if equations:
            eq_y = img_y
            for eq_idx, eq_path in enumerate(equations[:2]):
                try:
                    with open(eq_path, "rb") as f:
                        eq_data = base64.b64encode(f.read()).decode("utf-8")
                    from PIL import Image as PILImage
                    with PILImage.open(eq_path) as im:
                        eq_w, eq_h = im.size
                    scale = min(480 / max(eq_w, 1), 60 / max(eq_h, 1), 1.0)
                    disp_w = int(eq_w * scale)
                    disp_h = int(eq_h * scale)
                    eq_x = img_x + (520 - disp_w) // 2

                    equation_svg += f'''  <rect x="{img_x}" y="{eq_y}" width="520" height="{disp_h + 40}" rx="8" fill="{s['secondary_bg']}" stroke="{s['border']}" stroke-width="0.5"/>
  <image href="data:image/png;base64,{eq_data}" x="{eq_x}" y="{eq_y + 10}" width="{disp_w}" height="{disp_h}" preserveAspectRatio="xMidYMid meet"/>
  <text x="{img_x + 520 - 40}" y="{eq_y + disp_h + 30}" font-size="12" fill="{s['secondary_text']}" font-family="Microsoft YaHei, sans-serif">({eq_idx + 1})</text>
'''
                    eq_y += disp_h + 55
                except Exception as e:
                    print(f"[SVG]   公式 {eq_path} 编码失败: {e}")

    # ──── 组装完整 SVG ────
    svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 1280 720" width="1280" height="720">
  <defs>
    <linearGradient id="titleBar" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="{s['primary']}"/>
      <stop offset="100%" stop-color="{s['accent']}"/>
    </linearGradient>
  </defs>

  <!-- 背景 -->
  <rect width="1280" height="720" fill="{s['background']}"/>

  <!-- 顶部渐变条 -->
  <rect x="0" y="0" width="1280" height="5" fill="url(#titleBar)"/>

  <!-- 标题区背景 -->
  <rect x="0" y="5" width="1280" height="90" fill="{s['secondary_bg']}"/>

  <!-- 标题左侧强调条 -->
  <rect x="60" y="20" width="5" height="55" fill="{s['secondary_accent']}" rx="2"/>

  <!-- 标题文字 -->
  <text x="80" y="60" font-size="28" fill="{s['primary']}" font-family="Microsoft YaHei, sans-serif" font-weight="bold">{_xml_escape(title)}</text>

  <!-- 标题下划线 -->
  <rect x="80" y="72" width="100" height="3" rx="1" fill="{s['secondary_accent']}"/>

  <!-- 内容区 -->
{content_svg}

  <!-- 图片区 -->
{image_svg}

  <!-- 右下装饰 -->
  <circle cx="1240" cy="660" r="60" fill="{s['accent']}" fill-opacity="0.04"/>

  <!-- 底部条 -->
  <rect x="0" y="692" width="1280" height="28" fill="{s['primary']}"/>
  <text x="30" y="710" font-size="10" fill="#FFFFFF" fill-opacity="0.6" font-family="Microsoft YaHei, sans-serif">Pre-Assistant</text>
</svg>'''
    return svg


def _fallback_svg(slide_info, style):
    """生成简单的默认 SVG 页面。"""
    title = slide_info.get("title", "无标题")
    content = slide_info.get("content", "")
    is_section = title in {"研究背景", "研究方法", "实验与结果", "总结展望",
                           "背景与动机", "方法", "结果", "讨论", "结论"}
    is_cover = slide_info == list  # hack: first slide

    if is_section or (not content):
        # 章节页
        svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">
  <rect width="1280" height="720" fill="{style['primary']}"/>
  <rect x="80" y="280" width="6" height="160" fill="{style['secondary_accent']}"/>
  <text x="110" y="380" font-size="40" fill="#FFFFFF" font-family="Microsoft YaHei, sans-serif" font-weight="bold">{_xml_escape(title)}</text>
  <rect x="110" y="400" width="200" height="3" fill="{style['accent']}"/>
</svg>'''
    else:
        # 内容页
        lines = content.split("\n")
        text_elements = ""
        y_pos = 200
        for line in lines:
            line = line.strip().lstrip("- ").strip()
            if not line:
                continue
            prefix = ""
            if line.startswith(("•", "●")) or (len(line) > 2 and line[0].isdigit() and line[1] in ".、"):
                prefix = "▸ "
                line = line.lstrip("•●").strip()
            text_elements += f'  <text x="100" y="{y_pos}" font-size="18" fill="{style["body_text"]}" font-family="Microsoft YaHei, sans-serif">{prefix}{_xml_escape(line)}</text>\n'
            y_pos += 35

        svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">
  <rect width="1280" height="720" fill="{style['background']}"/>
  <rect x="0" y="0" width="1280" height="5" fill="{style['primary']}"/>
  <rect x="0" y="0" width="4" height="720" fill="{style['accent']}"/>
  <rect x="4" y="5" width="1276" height="80" fill="{style['secondary_bg']}"/>
  <text x="60" y="55" font-size="28" fill="{style['primary']}" font-family="Microsoft YaHei, sans-serif" font-weight="bold">{_xml_escape(title)}</text>
  <rect x="60" y="75" width="80" height="3" fill="{style['secondary_accent']}"/>
{text_elements}
  <rect x="0" y="690" width="1280" height="30" fill="{style['primary']}"/>
  <text x="30" y="710" font-size="10" fill="#AAAAAA" font-family="Microsoft YaHei, sans-serif">Pre-Assistant</text>
</svg>'''

    return svg


def _xml_escape(text):
    """XML 转义。"""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))


def _blank_png(w, h, bg_color):
    """生成纯色 PNG 字节。"""
    img = Image.new("RGB", (w, h), bg_color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _generate_ppt_png_fallback(svg_results, slides_data, style, output_path):
    """PNG 回退模式：resvg 渲染 SVG → PNG → 组装 PPTX。"""
    print("[SVG-PNG] 回退到 PNG 模式组装 PPTX...")
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank_layout = prs.slide_layouts[6]

    for idx, svg_code in enumerate(svg_results):
        svg_clean = _sanitize_svg(svg_code)
        try:
            png_bytes = svg_to_bytes(svg_clean)
        except Exception:
            fallback = _fallback_svg(slides_data[idx], style)
            try:
                png_bytes = svg_to_bytes(fallback)
            except Exception:
                png_bytes = _blank_png(1280, 720, style["primary"])

        slide = prs.slides.add_slide(blank_layout)
        img_stream = BytesIO(png_bytes)
        slide.shapes.add_picture(img_stream, Inches(0), Inches(0), Inches(13.333), Inches(7.5))

        notes = slides_data[idx].get("speaker_notes", "")
        if notes:
            slide.notes_slide.notes_text_frame.text = notes

        print(f"[SVG-PNG]   第{idx+1}页组装完成")

    prs.save(output_path)
    print(f"[SVG-PNG] 保存完成: {output_path}")

@app.route('/')
def index():
    return send_file('index.html')


@app.route("/extract", methods=["POST"])
def api_extract():
    if "pdf" not in request.files:
        return jsonify({"error": "请上传 PDF 文件"}), 400

    file = request.files["pdf"]
    session_id = str(uuid.uuid4())
    session_dir = os.path.join("sessions", session_id)
    os.makedirs(session_dir, exist_ok=True)

    pdf_path = os.path.join(session_dir, "input.pdf")
    file.save(pdf_path)

    extracted = extract_pdf(pdf_path, session_dir)
    sessions[session_id] = {"pdf_path": pdf_path, "extracted": extracted, "session_dir": session_dir}

    return jsonify({
        "session_id": session_id,
        "total_pages": extracted["total_pages"],
        "figures_count": len(extracted["figures"]),
        "equations_count": len(extracted["equations"]),
        "outline": extracted["outline"],
        "visual_assets": [
            {
                "type": a["type"],
                "index": a["index"],
                "page": a["page"],
                "preview": a.get("text_preview", ""),
                "url": f"/asset/{session_id}/{a['type']}/{a['filename']}"
            }
            for a in extracted["visual_assets"]
        ],
        "pages_preview": [
            {"page": p["page"], "length": len(p["text"])}
            for p in extracted["pages"] if p["text"]
        ]
    })


@app.route("/asset/<session_id>/<asset_type>/<filename>")
def api_asset(session_id, asset_type, filename):
    if session_id not in sessions:
        return jsonify({"error": "无效的会话ID"}), 400
    session_dir = sessions[session_id]["session_dir"]
    asset_dir = os.path.join(session_dir, asset_type + "s")
    return send_from_directory(asset_dir, filename)


@app.route("/outline", methods=["POST"])
def api_outline():
    data = request.get_json()
    session_id = data.get("session_id")
    if session_id not in sessions:
        return jsonify({"error": "无效的会话ID，请先上传PDF"}), 400

    session = sessions[session_id]
    result = ai_generate_outline(session["extracted"])

    session["outline"] = result
    session["reading_matrix"] = result.get("reading_matrix", {})

    return jsonify({
        "session_id": session_id,
        "reading_matrix": result.get("reading_matrix", {}),
        "slides": result.get("slides", [])
    })


@app.route("/generate", methods=["POST"])
def api_generate():
    data = request.get_json()
    session_id = data.get("session_id")
    mode = data.get("mode", "quick")  # "quick" or "fancy"
    style_key = data.get("style", "academic")  # "academic", "consulting", "tech", "nature"

    if session_id not in sessions:
        return jsonify({"error": "无效的会话ID"}), 400

    session = sessions[session_id]

    custom_slides = data.get("slides")
    slides = custom_slides if custom_slides else session.get("outline", {}).get("slides", [])

    if not slides:
        return jsonify({"error": "没有可用的提纲数据，请先生成提纲"}), 400

    output_path = os.path.join(session["session_dir"], "output.pptx")

    if mode == "fancy" and RESVG_AVAILABLE:
        try:
            generate_ppt_svg_mode(slides, session["extracted"], style_key=style_key, output_path=output_path)
        except Exception as e:
            print(f"[SVG] 精美模式失败，回退到快速模式: {e}")
            generate_ppt(slides, session["extracted"], "template.pptx", output_path)
    else:
        generate_ppt(slides, session["extracted"], "template.pptx", output_path)

    session["output_path"] = output_path
    session["final_slides"] = slides

    script_lines = []
    for i, s in enumerate(slides):
        va = s.get("visual_assets", [])
        va_desc = ""
        if va:
            parts = []
            for v in va:
                if v.get("type") == "figure":
                    parts.append(f"图{v.get('index')}")
                elif v.get("type") == "equation":
                    parts.append(f"公式{v.get('index')}")
            va_desc = "、".join(parts)

        script_lines.append({
            "page": i + 1,
            "title": s.get("title", ""),
            "notes": s.get("speaker_notes", ""),
            "orig": s.get("orig", ""),
            "visual_assets_desc": va_desc
        })

    return jsonify({
        "session_id": session_id,
        "slides_count": len(slides),
        "script_lines": script_lines,
        "message": "PPT生成成功！"
    })


@app.route("/download/<session_id>", methods=["GET"])
def api_download(session_id):
    if session_id not in sessions:
        return jsonify({"error": "无效的会话ID"}), 400

    output_path = sessions[session_id].get("output_path")
    if not output_path or not os.path.exists(output_path):
        return jsonify({"error": "PPT 尚未生成"}), 404

    return send_file(output_path, as_attachment=True, download_name="output.pptx")


# ====================== 启动 ======================
if __name__ == '__main__':
    os.makedirs("sessions", exist_ok=True)
    print("=" * 50)
    print("Pre-Assistant 服务已启动")
    print(f"API Key: {'已配置' if DOUBAO_API_KEY else '未配置'}")
    print(f"PaddleOCR: {'可用' if PADDLEOCR_AVAILABLE else '未安装（可选）'}")
    print("访问 http://127.0.0.1:5000")
    print("=" * 50)
    app.run(host="127.0.0.1", port=5000, debug=True)
