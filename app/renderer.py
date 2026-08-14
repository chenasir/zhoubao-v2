"""在原模板 docx 上做「就地替换」，严格保留封面、页眉页脚、样式、字体、目录结构。

模板结构（通过探测已确认）：
  ┌─ 封面（在 w:drawing → w:txbxContent 里）
  │    全球新闻摘要 / Global Markets News Digest
  │    第136期 / Issue 136
  │    2026年4月15日-2026年4月21日 / Apr 15, 2026 – Apr 21, 2026
  │
  ├─ 新闻目录 / Newsletter Highlights (标题段落)
  │    沙特阿拉伯 …………… 2
  │      标题1（style='Chin'）
  │      标题2（style='Chin'）
  │      ...
  │    阿联酋 …………… 2
  │      标题...
  │    ...
  │
  ├─ <空白/分页>
  │
  ├─ 正文：
  │    沙特阿拉伯（Heading 1，style='1' 或 '' 但文本恰好 == 国家名）
  │    标题1（Normal）
  │    正文1（Normal）
  │    ...
  │
  └─ 声明 / Disclaimer
         免责声明正文

本文件流程：
  1) 打开模板 → 克隆
  2) 在 document.xml + 所有 header/footer parts 上做「段落级聚合文本替换」：
     - 第136期 → 第{issue}期；Issue 136 → Issue {issue}
     - 日期范围
  3) 按「从后往前」的方法找正文锚点：以 "声明"/"Disclaimer" 为终点
     向前扫，收集文本 strip 后恰好等于国家名的段落作为国家正文锚点
  4) 删掉旧正文 → 克隆模板里的"标题段落样式"/"正文段落样式"，插入新内容
  5) 重建「新闻目录」区：
     - 定位 目录标题段落（"新闻目录" / "Newsletter Highlights"）
     - 目录区 = 该段之后到第一个正文锚点之前的所有段落
     - 记下原目录里的"国家目录行样式"(dotted leader 行) 和"标题行样式"(Chin 风格)
     - 删除旧目录区 → 克隆样式，按新数据重建
"""
from __future__ import annotations

import logging
import re
from copy import deepcopy
from datetime import date
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT

from .config import TEMPLATES_DIR
from .models import FormattedItem

logger = logging.getLogger(__name__)

TEMPLATE_EN = TEMPLATES_DIR / "template_en.docx"
TEMPLATE_CN = TEMPLATES_DIR / "template_cn.docx"

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NSMAP = {"w": W}

# 国家（正文锚点）文本 → country_code
EN_COUNTRY = {
    "Saudi Arabia": "KSA",
    "UAE": "UAE",
    "Kazakhstan": "KZ",
    "South Korea": "KR",
    "Korea": "KR",
}
ZH_COUNTRY = {
    "沙特阿拉伯": "KSA",
    "阿联酋": "UAE",
    "哈萨克斯坦": "KZ",
    "韩国": "KR",
}

# 目录行里国家名前缀的可能写法（EN 模板里 TOC 前缀是带空格的）
EN_TOC_COUNTRY = {
    "Saudi Arabia": "KSA",
    "UAE": "UAE",
    "Kazakhstan": "KZ",
    "South Korea": "KR",
}
ZH_TOC_COUNTRY = ZH_COUNTRY

COUNTRY_ORDER = ["KSA", "UAE", "KZ", "KR"]

# 用于显示的国家名
COUNTRY_DISPLAY = {
    "en": {"KSA": "Saudi Arabia", "UAE": "UAE", "KZ": "Kazakhstan", "KR": "South Korea"},
    "zh": {"KSA": "沙特阿拉伯", "UAE": "阿联酋", "KZ": "哈萨克斯坦", "KR": "韩国"},
}

ZH_TOC_LAYOUT = {
    "heading_row": 0,
    "countries": {
        "KSA": {"country_row": 1, "titles_row": 2},
        "UAE": {"country_row": 3, "titles_row": 4},
        "KZ": {"country_row": 5, "titles_row": 6, "titles_slice": (0, 4)},
        "KR": {"country_row": 6, "country_para_index": 4, "titles_row": 6, "titles_slice": (5, None)},
    },
}


# ---------------- 低层 XML 工具 ---------------- #

def xp(el, q):
    """对 lxml 元素做 xpath，w 命名空间固定。

    python-docx 的 BaseOxmlElement.xpath 已内置 w: 前缀（不接收 namespaces），
    而纯 lxml 元素需要传 namespaces。做个自适应。"""
    try:
        return el.xpath(q, namespaces=NSMAP)
    except TypeError:
        return el.xpath(q)


def _p_text(p_el) -> str:
    """段落所有 <w:t> 聚合文本。"""
    return "".join((t.text or "") for t in xp(p_el, ".//w:t"))


def _p_style(p_el) -> str:
    pPr = p_el.find(qn("w:pPr"))
    if pPr is None:
        return ""
    ps = pPr.find(qn("w:pStyle"))
    if ps is None:
        return ""
    return ps.get(qn("w:val")) or ""


def _set_text_nodes_preserve_structure(el, new_text: str) -> None:
    """尽量只改文字，不改原有 run / t 结构。

    做法：
    - 找到元素下所有 w:t
    - 按原始各 text 节点的长度把 new_text 分配回去
    - 保留原本的 run / rPr / pPr / hyperlink / bookmark / textbox 结构

    这样比“清空整段后只塞一个 run”更能保持模板原有的字体、字号和布局。
    """
    new_text = re.sub(r"[\r\n]+", " ", new_text or "").strip()
    ts = xp(el, ".//w:t")
    if not ts:
        return

    old_texts = [(t.text or "") for t in ts]
    old_lens = [len(s) for s in old_texts]

    if len(ts) == 1:
        ts[0].text = new_text
        if new_text.startswith(" ") or new_text.endswith(" "):
            ts[0].set(qn("xml:space"), "preserve")
        return

    pos = 0
    for i, t in enumerate(ts):
        if i < len(ts) - 1:
            step = old_lens[i]
            chunk = new_text[pos: pos + step]
            pos += step
        else:
            chunk = new_text[pos:]
        t.text = chunk
        if chunk.startswith(" ") or chunk.endswith(" "):
            t.set(qn("xml:space"), "preserve")


def _strip_hyperlinks(p_el) -> None:
    """把段落中的 w:hyperlink 拆解为普通 run，彻底消除蓝色/下划线样式。

    超链接 run 的蓝色来自 rStyle 引用（中文模板是 "a9"，英文模板是 "Hyperlink"），
    而非直接在 rPr 里写 w:color。因此必须无条件删除 rStyle / color / underline。
    """
    for hl in list(xp(p_el, ".//w:hyperlink")):
        parent = hl.getparent()
        for child in list(hl):
            hl.addprevious(child)
            if child.tag == qn("w:r"):
                rPr = child.find(qn("w:rPr"))
                if rPr is not None:
                    for tag in (qn("w:rStyle"), qn("w:color"), qn("w:u")):
                        el = rPr.find(tag)
                        if el is not None:
                            rPr.remove(el)
        parent.remove(hl)


def _set_p_text(p_el, new_text: str) -> None:
    """只改段落文本，尽量保留原有 run 结构。先清除超链接避免蓝色残留。"""
    _strip_hyperlinks(p_el)
    _set_text_nodes_preserve_structure(p_el, new_text)


def _append_hyperlink(p_el, doc_part, url: str, text: str) -> None:
    if not url or not text:
        return
    r_id = doc_part.relate_to(url, RT.HYPERLINK, is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)
    run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    fonts = OxmlElement("w:rFonts")
    fonts.set(qn("w:ascii"), "Arial")
    fonts.set(qn("w:hAnsi"), "Arial")
    rPr.append(fonts)
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "1F4E79")
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    rPr.append(color)
    rPr.append(underline)
    text_el = OxmlElement("w:t")
    text_el.text = text
    run.append(rPr)
    run.append(text_el)
    hyperlink.append(run)
    p_el.append(hyperlink)


def _append_plain_text(p_el, text: str) -> None:
    if not text:
        return
    run = OxmlElement("w:r")
    existing_runs = xp(p_el, ".//w:r[not(ancestor::w:hyperlink)]")
    if existing_runs:
        rPr = existing_runs[-1].find(qn("w:rPr"))
        if rPr is not None:
            run.append(deepcopy(rPr))
    text_el = OxmlElement("w:t")
    text_el.text = text
    if text.startswith(" ") or text.endswith(" "):
        text_el.set(qn("xml:space"), "preserve")
    run.append(text_el)
    p_el.append(run)


def _source_name_from_label(source_label: str) -> str:
    label = (source_label or "").strip()
    if not label:
        return ""
    match = re.search(r"(?:来源|Source)\s*[:：]\s*([^,，)）]+)", label, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    cleaned = label.strip("()（） ")
    cleaned = re.sub(r"^(?:来源|Source)\s*[:：]\s*", "", cleaned, flags=re.IGNORECASE)
    return re.split(r"[,，)）]", cleaned, maxsplit=1)[0].strip()


def _set_p_text_with_optional_hyperlink(p_el, new_text: str, source_label: str, source_url: str, doc_part) -> None:
    if source_label and source_url and source_label in new_text:
        label_start = new_text.rfind(source_label)
        source_name = _source_name_from_label(source_label)
        link_offset = source_label.find(source_name) if source_name else -1
        if link_offset < 0:
            _set_p_text(p_el, new_text)
            return
        link_start = label_start + link_offset
        link_end = link_start + len(source_name)
        prefix = new_text[:link_start]
        suffix = new_text[link_end:]
        _set_p_text(p_el, prefix)
        _append_hyperlink(p_el, doc_part, source_url, source_name)
        _append_plain_text(p_el, suffix)
        return
    _set_p_text(p_el, new_text)


def _is_structural_paragraph(p_el) -> bool:
    return bool(xp(p_el, ".//w:sectPr") or xp(p_el, ".//w:br[@w:type='page']"))


def _remove_empty_paragraphs(elements: list[object]) -> None:
    """删除模板留下的纯空占位段，保留分节/分页段。"""
    for el in list(elements):
        if el.tag != qn("w:p"):
            continue
        if _p_text(el).strip() or _is_structural_paragraph(el):
            continue
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)


def _ensure_page_break_before(p_el) -> None:
    """确保该段落从新页开始。用于英文 Disclaimer 单独成页。"""
    pPr = p_el.find(qn("w:pPr"))
    if pPr is None:
        pPr = OxmlElement("w:pPr")
        p_el.insert(0, pPr)

    page_break_before = pPr.find(qn("w:pageBreakBefore"))
    if page_break_before is None:
        page_break_before = OxmlElement("w:pageBreakBefore")
        pPr.append(page_break_before)
    page_break_before.set(qn("w:val"), "1")


def _clear_page_break_before(p_el) -> None:
    pPr = p_el.find(qn("w:pPr"))
    if pPr is None:
        return
    page_break_before = pPr.find(qn("w:pageBreakBefore"))
    if page_break_before is not None:
        pPr.remove(page_break_before)


def _find_prev_section_break_paragraph(p_el):
    """向前找到最近一个承载 sectPr 的段落。"""
    prev = p_el.getprevious()
    while prev is not None:
        if prev.tag == qn("w:p"):
            pPr = prev.find(qn("w:pPr"))
            sectPr = pPr.find(qn("w:sectPr")) if pPr is not None else None
            if sectPr is not None:
                return prev
        prev = prev.getprevious()
    return None


def _ensure_disclaimer_starts_new_page(body_el, disclaimer_p):
    """让 Disclaimer 单独起新页，但只使用其前方的模板分节段，不额外挤占正文。

    英文模板在 Disclaimer 前通常已有一个空段落承载 sectPr。优先把该 sectPr
    显式设为 nextPage；若没找到，再退回到给 Disclaimer 标题加 pageBreakBefore。
    """
    prev = _find_prev_section_break_paragraph(disclaimer_p)
    if prev is not None:
        pPr = prev.find(qn("w:pPr"))
        sectPr = pPr.find(qn("w:sectPr")) if pPr is not None else None
        if sectPr is not None:
            sect_type = sectPr.find(qn("w:type"))
            if sect_type is None:
                sect_type = OxmlElement("w:type")
                sectPr.insert(0, sect_type)
            sect_type.set(qn("w:val"), "nextPage")
            _clear_page_break_before(disclaimer_p)
            return prev

    _ensure_page_break_before(disclaimer_p)
    return None


def _ensure_paragraph_precedes(target_p, anchor_p) -> None:
    """若 target_p 不紧邻 anchor_p 前方，则移动到 anchor_p 正前。"""
    if target_p is None or anchor_p is None:
        return
    if target_p.getnext() is anchor_p:
        return
    parent = target_p.getparent()
    if parent is None:
        return
    parent.remove(target_p)
    anchor_p.addprevious(target_p)


def _pop_sectpr(p_el):
    """取下段落上的 sectPr 并返回拷贝。"""
    pPr = p_el.find(qn("w:pPr"))
    if pPr is None:
        return None
    sectPr = pPr.find(qn("w:sectPr"))
    if sectPr is None:
        return None
    copied = deepcopy(sectPr)
    pPr.remove(sectPr)
    return copied


def _append_sectpr(p_el, sectpr_el) -> None:
    """把 sectPr 挂到目标段落上。"""
    if sectpr_el is None:
        return
    pPr = p_el.find(qn("w:pPr"))
    if pPr is None:
        pPr = OxmlElement("w:pPr")
        p_el.insert(0, pPr)
    old = pPr.find(qn("w:sectPr"))
    if old is not None:
        pPr.remove(old)
    pPr.append(deepcopy(sectpr_el))


def _replace_country_row_text(old_text: str, old_country_names: set[str], new_country: str, new_page: int) -> str:
    """尽量保留目录行里原本的 leader / P 前缀，仅替换国家名和页码。"""
    old_country = next((name for name in sorted(old_country_names, key=len, reverse=True) if old_text.startswith(name)), "")
    m = re.search(r"(\d+)\s*$", old_text)
    if old_country and m:
        middle = old_text[len(old_country): m.start(1)]
        return f"{new_country}{middle}{new_page}"
    return f"{new_country}{old_text[len(old_country):] if old_country else ' ' + ('…' * 25) + ' P'}{new_page}"


def _replace_in_subtree(root_el, replacers) -> int:
    """对 root 下所有 <w:p> 做段落级文本替换：聚合 w:t → 用 replacers 改写 → 写回首个 w:t。

    replacers: list[tuple[re.Pattern|str, str]] 形式的 (pattern, replacement)
    返回替换命中段落数（仅做统计）。
    """
    hit = 0
    for p in xp(root_el, ".//w:p"):
        # 若该段落是 textbox 的“宿主段落”（例如封面/页眉里的 drawing 容器），
        # 直接改它会把内部 txbxContent 文本全部串起来写回首个 w:t，破坏封面结构。
        # 这里跳过宿主段落，只处理真正的 txbxContent 内部段落。
        if xp(p, ".//w:txbxContent"):
            continue
        ts = xp(p, ".//w:t")
        if not ts:
            continue
        original = "".join((t.text or "") for t in ts)
        new = original
        for pat, repl in replacers:
            if isinstance(pat, str):
                new = new.replace(pat, repl)
            else:
                new = pat.sub(repl, new)
        if new != original:
            _set_text_nodes_preserve_structure(p, new)
            hit += 1
    return hit


# ---------------- 日期格式化 ---------------- #

def _zh_date(d: date) -> str:
    return f"{d.year}年{d.month}月{d.day}日"


def _en_date_short(d: date) -> str:
    """Apr 15, 2026 - 与模板一致（带英文月份缩写）。"""
    return d.strftime("%b %d, %Y").replace(" 0", " ")


def _en_date_long(d: date) -> str:
    """April 15, 2026"""
    return d.strftime("%B %d, %Y").replace(" 0", " ")


# ---------------- 克隆段落工具 ---------------- #

def _clone_p_with_text(template_p, new_text: str):
    """克隆一个段落，只替换文本，尽量保留原段落内部全部 run 结构。"""
    new_p = deepcopy(template_p)
    _strip_hyperlinks(new_p)
    _set_text_nodes_preserve_structure(new_p, new_text)
    return new_p


# ---------------- 正文锚点识别 ---------------- #

def _find_body_country_anchors(body_el, country_map: dict[str, str]):
    """在 body 里（非 txbxContent 内）找文本恰好等于国家名的段落，作为正文锚点。

    - 目录区里国家名带 '…' 和页码，strip() 后也不会等于国家名，自然被过滤。
    - 返回 (list of (code, p_element)) 按文档顺序；每个国家取第一个命中。
    - 同时识别 disclaimer/声明 段落。
    """
    anchors: list[tuple[str, object]] = []
    seen_codes: set[str] = set()
    for p in xp(body_el, ".//w:p"):
        # 跳过在 txbxContent（封面/页眉文本框）里的段落
        if xp(p, "ancestor::w:txbxContent"):
            continue
        text = _p_text(p).strip()
        if text in country_map:
            code = country_map[text]
            if code not in seen_codes:
                anchors.append((code, p))
                seen_codes.add(code)
    return anchors


def _normalize_country_heading_styles(anchors: list[tuple[str, object]]) -> None:
    """把所有国家标题段落统一成首个国家标题的段落样式。

    中文模板里 `阿联酋` 这一行本身样式异常，若直接沿用模板会和其它 H1 标题不一致。
    这里仅复制标题段落的 pPr（段落属性），不改文字内容，从而把它统一成和
    `沙特阿拉伯` 一样的 H1 外观。
    """
    if len(anchors) < 2:
        return

    ref_p = anchors[0][1]
    ref_children = [deepcopy(child) for child in ref_p]
    if not ref_children:
        return

    for _, p in anchors[1:]:
        old_text = _p_text(p).strip()
        for child in list(p):
            p.remove(child)
        for child in [deepcopy(c) for c in ref_children]:
            p.append(child)
        _set_p_text(p, old_text)


def _find_disclaimer(body_el, disclaimer_word: str):
    for p in xp(body_el, ".//w:p"):
        if xp(p, "ancestor::w:txbxContent"):
            continue
        text = _p_text(p).strip()
        if text == disclaimer_word:
            return p
    return None


def _find_highlights_heading(body_el, titles: list[str]):
    """找目录总标题（『新闻目录』/『Newsletter Highlights』）。"""
    target = [t.strip() for t in titles]
    for p in xp(body_el, ".//w:p"):
        if xp(p, "ancestor::w:txbxContent"):
            continue
        text = _p_text(p).strip()
        if text in target:
            return p
    return None


# ---------------- 同级兄弟删除 ---------------- #

def _siblings_between(start_el, stop_el):
    """返回 (start, stop) 开区间内的同级兄弟元素列表。"""
    out = []
    cur = start_el.getnext()
    while cur is not None and cur is not stop_el:
        out.append(cur)
        cur = cur.getnext()
    return out


def _delete_siblings_between(start_el, stop_el) -> None:
    for el in _siblings_between(start_el, stop_el):
        el.getparent().remove(el)


# ---------------- 页眉页脚替换 ---------------- #

def _apply_header_footer_replacements(doc, replacers) -> None:
    """遍历所有 section 的 header / footer（含 first_page、even），做段落级替换。"""
    for section in doc.sections:
        for part in (section.header, section.footer,
                     section.first_page_header, section.first_page_footer,
                     section.even_page_header, section.even_page_footer):
            try:
                el = part._element
            except AttributeError:
                continue
            if el is None:
                continue
            _replace_in_subtree(el, replacers)


# ---------------- 目录区重建 ---------------- #

def _rebuild_toc(
    body_el,  # unused but kept for API stability
    highlights_p,
    country_items: list[tuple[str, str, list[str], int]],
    lang: str,
    toc_leader: str,
) -> None:
    """重建『新闻目录』区。

    模板结构（已探测确认）：
      <w:tbl>
        <w:tr><w:tc><w:p>新闻目录</w:p></w:tc></w:tr>    ← 目录标题行
        <w:tr><w:tc><w:p>沙特阿拉伯……P2</w:p></w:tc></w:tr>  ← 国家目录行
        <w:tr><w:tc><w:p>标题1</w:p><w:p>标题2</w:p>...</w:tc></w:tr>  ← 该国标题列表
        <w:tr><w:tc><w:p>阿联酋……P3</w:p></w:tc></w:tr>
        ...
      </w:tbl>

    策略：
      - 定位 highlights_p 所在的 tr（heading_tr），作为"保留点"
      - 在它之后寻找两种 tr 作为样式模板：
          * country_row_tr_tpl —— 首段含"…"且以国家名开头
          * title_list_tr_tpl  —— 其它（含若干新闻标题）
      - 删除 heading_tr 之后所有 tr
      - 按 country_items 依次：克隆 country_row_tr 填国家目录行，
                              克隆 title_list_tr 填入对应国家全部标题
    """
    tc = highlights_p.getparent()
    if tc is None or tc.tag != qn("w:tc"):
        logger.warning("目录标题段落不在表格单元格里，跳过 TOC 重建（%s）", lang)
        return
    heading_tr = tc.getparent()
    if heading_tr is None or heading_tr.tag != qn("w:tr"):
        logger.warning("目录标题的 tc 没有父 tr，跳过 TOC 重建（%s）", lang)
        return
    tbl = heading_tr.getparent()
    if tbl is None or tbl.tag != qn("w:tbl"):
        logger.warning("目录标题 tr 没有父 tbl，跳过 TOC 重建（%s）", lang)
        return

    trs = xp(tbl, "./w:tr")
    try:
        heading_idx = trs.index(heading_tr)
    except ValueError:
        logger.warning("heading_tr 不在 tbl 子级里，跳过 TOC 重建（%s）", lang)
        return

    country_set = set(ZH_COUNTRY.keys()) | set(EN_COUNTRY.keys())

    def _is_country_row_tr(t) -> bool:
        ps = xp(t, ".//w:p")
        if not ps:
            return False
        text = _p_text(ps[0]).strip()
        if not text:
            return False
        has_dots = ("…" in text) or (re.search(r"\.{2,}", text) is not None)
        starts_with_country = any(text.startswith(c) for c in country_set)
        return has_dots and starts_with_country

    country_tr_tpl = None
    title_tr_tpl = None
    for t in trs[heading_idx + 1:]:
        if _is_country_row_tr(t) and country_tr_tpl is None:
            country_tr_tpl = t
        elif (not _is_country_row_tr(t)) and title_tr_tpl is None:
            # 只有含文字的 tr 才算标题列表样例
            all_t = xp(t, ".//w:t")
            if any((x.text or "").strip() for x in all_t):
                title_tr_tpl = t
        if country_tr_tpl is not None and title_tr_tpl is not None:
            break

    if country_tr_tpl is None:
        logger.warning("目录区没识别出『国家目录行 tr』样例，跳过 TOC 重建（%s）", lang)
        return

    country_tr_snapshot = deepcopy(country_tr_tpl)
    title_tr_snapshot = deepcopy(title_tr_tpl) if title_tr_tpl is not None else None

    # 删除 heading_tr 之后的所有 tr
    for t in trs[heading_idx + 1:]:
        tbl.remove(t)

    # 按国家插入
    anchor = heading_tr
    for code, display, titles, page_num in country_items:
        # 国家目录行 tr
        new_country_tr = deepcopy(country_tr_snapshot)
        first_p = xp(new_country_tr, ".//w:p")[0]
        _set_p_text(first_p, f"{display}{toc_leader}{page_num}")
        anchor.addnext(new_country_tr)
        anchor = new_country_tr

        if title_tr_snapshot is not None and titles:
            new_title_tr = deepcopy(title_tr_snapshot)
            # 在 tc 里以首个段落为样式模板重建
            cell = xp(new_title_tr, ".//w:tc")[0]
            cell_ps = xp(cell, "./w:p")
            if cell_ps:
                tpl_p = deepcopy(cell_ps[0])
                for p in cell_ps:
                    cell.remove(p)
                for t_text in titles:
                    np = _clone_p_with_text(tpl_p, t_text)
                    cell.append(np)
            anchor.addnext(new_title_tr)
            anchor = new_title_tr


def _clear_country_body_region(region: list[object]) -> None:
    """删除国家正文区内所有非布局段落（含模板样例新闻），保留分节/分页结构。"""
    for el in list(region):
        if el.tag != qn("w:p"):
            continue
        if _is_structural_paragraph(el):
            continue
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)


def _clear_cell_paragraphs(cell_el) -> None:
    for p in list(xp(cell_el, "./w:p")):
        cell_el.remove(p)


def _clear_tr_paragraphs(tr_el) -> None:
    for p in xp(tr_el, ".//w:p"):
        _set_p_text(p, "")


def _rebuild_toc_cn_in_place(root_el, items_by_country: dict[str, list[FormattedItem]]) -> None:
    """中文模板目录区按原模板的真实行/段结构原位改写。

    `template_cn.docx` 的目录表格并不是标准 4 国 * 2 行：
    - KSA: row1 / row2
    - UAE: row3 / row4
    - KZ : row5 / row6(前半)
    - KR : row6(中后半，country row + titles 都在同一 cell 里)

    为了最大程度保留模板布局，这里不重建整张表，只重写既有 row / cell / paragraph。
    """
    tbls = xp(root_el, ".//w:tbl")
    if not tbls:
        logger.warning("中文模板没找到目录表格，跳过目录重写")
        return
    tbl = tbls[0]
    trs = xp(tbl, "./w:tr")
    if len(trs) < 7:
        logger.warning("中文模板目录表格行数异常，跳过目录重写")
        return

    country_names = set(ZH_COUNTRY.keys())
    display_map = COUNTRY_DISPLAY["zh"]
    layout = ZH_TOC_LAYOUT["countries"]
    page_map = {"KSA": 2, "UAE": 3, "KZ": 4, "KR": 5}

    # 1) 单独处理三个独立国家行：KSA / UAE / KZ
    for code in ("KSA", "UAE", "KZ"):
        row_idx = layout[code]["country_row"]
        tr = trs[row_idx]
        if not items_by_country.get(code):
            _clear_tr_paragraphs(tr)
            continue
        ps = [p for p in xp(tr, ".//w:p") if _p_text(p).strip()]
        if not ps:
            continue
        p = ps[0]
        old_text = _p_text(p)
        _set_p_text(p, _replace_country_row_text(old_text, country_names, display_map[code], page_map[code]))

    # 2) KSA / UAE 标题列表行
    for code in ("KSA", "UAE"):
        row_idx = layout[code]["titles_row"]
        tr = trs[row_idx]
        cell = xp(tr, ".//w:tc")[0]
        if not items_by_country.get(code):
            _clear_cell_paragraphs(cell)
            continue
        old_ps = [p for p in xp(cell, "./w:p") if _p_text(p).strip()]
        if not old_ps:
            continue
        title_tpl = deepcopy(old_ps[0])
        _clear_cell_paragraphs(cell)
        for it in items_by_country.get(code, []):
            cell.append(_clone_p_with_text(title_tpl, it.cn_title))

    # 3) row6：前半是 KZ titles，后半是 KR country + KR titles
    mixed_tr = trs[layout["KZ"]["titles_row"]]
    mixed_cell = xp(mixed_tr, ".//w:tc")[0]
    mixed_ps_all = list(xp(mixed_cell, "./w:p"))
    mixed_ps_nonempty = [p for p in mixed_ps_all if _p_text(p).strip()]
    kz_items = items_by_country.get("KZ", [])
    kr_items = items_by_country.get("KR", [])
    if not kz_items and not kr_items:
        _clear_cell_paragraphs(mixed_cell)
        return
    if mixed_ps_nonempty:
        kz_title_tpl = deepcopy(mixed_ps_nonempty[0])
        kr_country_tpl = deepcopy(mixed_ps_nonempty[layout["KR"]["country_para_index"]])
        kr_title_tpl = deepcopy(mixed_ps_nonempty[layout["KR"]["titles_slice"][0]])

        _clear_cell_paragraphs(mixed_cell)

        for it in kz_items:
            mixed_cell.append(_clone_p_with_text(kz_title_tpl, it.cn_title))

        if kr_items:
            kr_country_p = _clone_p_with_text(
                kr_country_tpl,
                _replace_country_row_text(_p_text(kr_country_tpl), country_names, display_map["KR"], page_map["KR"]),
            )
            mixed_cell.append(kr_country_p)
            for it in kr_items:
                mixed_cell.append(_clone_p_with_text(kr_title_tpl, it.cn_title))


# ---------------- 正文重建 ---------------- #

def _rebuild_body(
    anchors: list[tuple[str, object]],
    disclaimer_p,
    disclaimer_sep_p,
    items_by_country: dict[str, list[FormattedItem]],
    lang: str,
    doc_part,
) -> None:
    """按国家正文锚点 + 免责声明，原位改写正文区。

    关键原则：
    - 不删除正文段落
    - 不删除任何 section break / columns / 空白占位段
    - 仅把模板中原有“标题/正文槽位”的文字改成新内容

    这样能最大程度保留模板原有的双栏、分页、段落定位和节分隔。
    """
    if not anchors:
        raise RuntimeError("模板里没找到任何正文国家锚点")

    # 构建 anchor→next_anchor 映射
    elements = [p for _, p in anchors] + [disclaimer_p]
    next_of = {elements[i]: elements[i + 1] for i in range(len(elements) - 1)}

    for code, anchor_el in anchors:
        region = _siblings_between(anchor_el, next_of[anchor_el])
        _remove_empty_paragraphs(region)
        region = _siblings_between(anchor_el, next_of[anchor_el])

        country_items = items_by_country.get(code, [])
        if not country_items:
            _clear_country_body_region(region)
            continue

        nonempty_slots = [p for p in region if p.tag == qn("w:p") and _p_text(p).strip()]
        if len(nonempty_slots) < 2:
            logger.warning("Country %s has no body template slots, skip rebuild", code)
            continue

        title_sample = deepcopy(nonempty_slots[0])
        body_sample = deepcopy(nonempty_slots[1]) if len(nonempty_slots) >= 2 else deepcopy(nonempty_slots[0])

        rewritten_entries: list[dict[str, str]] = []
        for it in country_items:
            title_text = it.en_title if lang == "en" else it.cn_title
            body_text = (it.en_body if lang == "en" else it.cn_body).rstrip()
            source_label = it.source_label_en if lang == "en" else it.source_label_zh
            if source_label and source_label not in body_text:
                body_text = body_text + " " + source_label
            rewritten_entries.extend(
                [
                    {"text": title_text, "source_label": "", "source_url": ""},
                    {"text": body_text, "source_label": source_label, "source_url": it.source_url or it.url},
                ]
            )

        insert_before = next_of[anchor_el]
        sectpr_host = None
        moved_sectpr = None
        if disclaimer_sep_p is not None and disclaimer_sep_p in region:
            insert_before = disclaimer_sep_p
        for p in region:
            if p.tag != qn("w:p"):
                continue
            if p.xpath("./w:pPr/w:sectPr"):
                sectpr_host = p
                insert_before = p
                break

        if sectpr_host is not None and sectpr_host in nonempty_slots and _p_text(sectpr_host).strip():
            moved_sectpr = _pop_sectpr(sectpr_host)
            insert_before = next_of[anchor_el]
            if disclaimer_sep_p is not None and disclaimer_sep_p in region:
                insert_before = disclaimer_sep_p

        for el in list(region):
            if el.tag != qn("w:p"):
                continue
            if _is_structural_paragraph(el):
                continue
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)

        last_new_body = None
        for i in range(0, len(rewritten_entries), 2):
            title_entry = rewritten_entries[i]
            body_entry = rewritten_entries[i + 1] if i + 1 < len(rewritten_entries) else {"text": "", "source_label": "", "source_url": ""}
            new_title = _clone_p_with_text(title_sample, title_entry["text"])
            new_body = _clone_p_with_text(body_sample, body_entry["text"])
            _set_p_text_with_optional_hyperlink(
                new_body,
                body_entry["text"],
                body_entry["source_label"],
                body_entry["source_url"],
                doc_part,
            )
            insert_before.addprevious(new_title)
            insert_before.addprevious(new_body)
            last_new_body = new_body

        if moved_sectpr is not None and last_new_body is not None:
            _append_sectpr(last_new_body, moved_sectpr)


# ---------------- 主流程 ---------------- #

def _render_one(
    template_path: Path,
    lang: str,  # 'en' / 'zh'
    items: list[FormattedItem],
    issue_number: int,
    start_date: date,
    end_date: date,
    out_path: Path,
) -> Path:
    if not template_path.exists():
        raise FileNotFoundError(f"模板不存在: {template_path}")

    doc = Document(template_path)
    body = doc.element.body

    country_map = EN_COUNTRY if lang == "en" else ZH_COUNTRY
    disclaimer_word = "Disclaimer" if lang == "en" else "声明"
    highlights_titles = (["Newsletter Highlights", "Table of Contents", "Contents"]
                         if lang == "en" else ["新闻目录", "目录"])

    # -------- 构造替换规则（期号 + 日期）-------- #
    replacers: list = []

    # 期号
    replacers.append((re.compile(r"第\s*\d+\s*期"), f"第{issue_number}期"))
    replacers.append((re.compile(r"Issue\s+\d+", re.IGNORECASE), f"Issue {issue_number}"))

    # 日期：模板里有若干风格，尽量多覆盖
    if lang == "en":
        new_range = f"{_en_date_short(start_date)} – {_en_date_short(end_date)}"
        new_range_long = f"{_en_date_long(start_date)} - {_en_date_long(end_date)}"
        # 匹配 "Apr 15, 2026 – Apr 21, 2026" / "April 15, 2026 - April 21, 2026"
        # 允许连字符、短破折号(–)、长破折号(—)、波浪号、空格组合
        en_range_re = re.compile(
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|January|February|March|April|"
            r"June|July|August|September|October|November|December)\s+\d{1,2},\s*\d{4}\s*[-–—~]\s*"
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|January|February|March|April|"
            r"June|July|August|September|October|November|December)\s+\d{1,2},\s*\d{4}"
        )
        replacers.append((en_range_re, new_range))
        # 第二套：有些地方写成 long 形式，额外做一次覆盖（已被上面兜住）
        _ = new_range_long
    else:
        new_range = f"{_zh_date(start_date)}-{_zh_date(end_date)}"
        # 匹配 "2026年4月15日-2026年4月21日"（横杠可以是 - – — ~）
        zh_range_re = re.compile(
            r"\d{4}年\d{1,2}月\d{1,2}日\s*[-–—~]\s*\d{4}年\d{1,2}月\d{1,2}日"
        )
        replacers.append((zh_range_re, new_range))

    # -------- 1) body + header/footer 替换（封面、目录标题、页眉日期都在内）-------- #
    _replace_in_subtree(body, replacers)
    _apply_header_footer_replacements(doc, replacers)

    # -------- 2) 解析正文锚点 -------- #
    anchors = _find_body_country_anchors(body, country_map)
    disclaimer_p = _find_disclaimer(body, disclaimer_word)
    if disclaimer_p is None:
        raise RuntimeError(f"模板里没找到 {disclaimer_word} 段落: {template_path.name}")

    if lang == "zh":
        _normalize_country_heading_styles(anchors)
        disclaimer_sep_p = None
    else:
        disclaimer_sep_p = _ensure_disclaimer_starts_new_page(body, disclaimer_p)
        _ensure_paragraph_precedes(disclaimer_sep_p, disclaimer_p)

    # 正文必须保留模板原有全部国家锚点，否则容易丢 section break / 分栏结构
    anchors_in_order = anchors

    if not anchors_in_order:
        raise RuntimeError("没找到任何国家锚点（可能本期数据全空）")

    # -------- 3) 分组数据 -------- #
    items_by_country: dict[str, list[FormattedItem]] = {}
    for it in items:
        items_by_country.setdefault(it.country_code, []).append(it)

    # -------- 4) 重建正文 -------- #
    _rebuild_body(anchors_in_order, disclaimer_p, disclaimer_sep_p, items_by_country, lang, doc.part)

    # -------- 5) 重建目录 -------- #
    if lang == "zh":
        _rebuild_toc_cn_in_place(body, items_by_country)
    else:
        highlights_p = _find_highlights_heading(body, highlights_titles)
        if highlights_p is None:
            logger.warning("没找到目录标题段落，跳过目录重建: %s", template_path.name)
        else:
            display_map = COUNTRY_DISPLAY["en"]
            leader = " " + ("…" * 25) + " "
            base_page = 2
            country_items = []
            for idx, code in enumerate(COUNTRY_ORDER):
                if code not in items_by_country:
                    continue
                titles = [it.en_title for it in items_by_country[code]]
                country_items.append((code, display_map[code], titles, base_page + idx))
            _rebuild_toc(
                body,
                highlights_p,
                country_items,
                lang,
                leader,
            )

    # -------- 保存 -------- #
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_path)
    return out_path


def render_bilingual(
    items: list[FormattedItem],
    issue_number: int,
    start_date: date,
    end_date: date,
    out_dir: Path,
) -> tuple[Path, Path]:
    en_path = out_dir / f"[CICC] Weekly Global Markets News Digest_Issue_{issue_number}_ENG.docx"
    cn_path = out_dir / f"【中金国际】全球周度新闻摘要_第{issue_number}期.docx"
    _render_one(TEMPLATE_EN, "en", items, issue_number, start_date, end_date, en_path)
    _render_one(TEMPLATE_CN, "zh", items, issue_number, start_date, end_date, cn_path)
    return en_path, cn_path
