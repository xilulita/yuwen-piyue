# -*- coding: utf-8 -*-
"""
智批学练 · 语文试卷 AI 批改与学情训练系统（本地版）
功能：试卷批改 -> 错因分析 -> 学情档案 -> 按薄弱知识点生成同类训练题 -> 一键导出纯训练题 PDF
运行：python app.py   （仅依赖 reportlab，其余为标准库）
"""
import json
import os
import re
import io
import random
import threading
import time
import uuid
import webbrowser
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_FILE = os.path.join(BASE_DIR, "data.json")
LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# 题库：语文 14 类考点，参数化生成器 gen(rng) -> (题干, 参考答案)
# 同一 seed 可确定性复现，用于服务端批改校验与变式训练出题
# ---------------------------------------------------------------------------

from yuwen_bank import TEMPLATES  # 语文题库（14 类考点）

def generate_question(kp, seed):
    tpl = TEMPLATES[kp]
    rng = random.Random(seed)
    stem, ans = tpl["gen"](rng)
    return {"kp": kp, "kp_name": tpl["name"], "seed": seed, "stem": stem, "answer": ans}

# ---------------------------------------------------------------------------
# 答案归一化与判分（语文：忽略空白、全半角标点差异、书名号有无等）
# ---------------------------------------------------------------------------

def normalize_answer(text):
    """归一化语文答案：去空白；全角标点转半角；去掉外围书名号引号；末尾句号宽容。"""
    if text is None:
        return None
    s = str(text).strip()
    if s == "":
        return None
    trans = str.maketrans("０１２３４５６７８９（）：，。！？；、．", "0123456789():,.!?;,.")
    s = s.translate(trans)
    s = re.sub(r"\s+", "", s)
    s = s.strip("《》\"'“”‘’")
    s = s.rstrip("。.")
    return s

def answer_value(s):
    try:
        if "/" in s and re.fullmatch(r"-?\d+/\d+", s):
            return Fraction(s)
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            return Fraction(s)
    except (ValueError, ZeroDivisionError):
        pass
    return None

def is_correct(student, expected):
    a, b = normalize_answer(student), normalize_answer(expected)
    if a is None or b is None:
        return False
    if a == b:
        return True
    # 参考答案含多个可接受写法（用 / 或 或 分隔，如近义词）
    for alt in re.split(r"[/或]", b):
        alt = alt.strip()
        if alt and a == alt:
            return True
    va, vb = answer_value(a), answer_value(b)
    if va is not None and vb is not None:
        return va == vb
    return False

# ---------------------------------------------------------------------------
# 数据存储（学情档案持久化到 data.json）
# ---------------------------------------------------------------------------

def empty_db():
    return {"students": [], "exams": [], "trainings": [], "answer_keys": []}

def load_db():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                db = json.load(f)
            for k in empty_db():
                db.setdefault(k, [])
            return db
        except (json.JSONDecodeError, OSError):
            pass
    return empty_db()

def save_db(db):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA_FILE)

DB = load_db()

def new_id(prefix):
    return prefix + "-" + uuid.uuid4().hex[:10]

# ---------------------------------------------------------------------------
# 试卷批改与学情分析
# ---------------------------------------------------------------------------

def build_paper(kp_list, count_per_kp, seed_base):
    """从所选知识点生成一份试卷（每题带 seed，可服务端校验答案）。"""
    questions = []
    rng = random.Random(seed_base)
    for kp in kp_list:
        used = set()
        for _ in range(count_per_kp):
            while True:
                seed = rng.randint(1, 10**9)
                q = generate_question(kp, seed)
                if q["stem"] not in used:
                    used.add(q["stem"])
                    break
            questions.append(q)
    return questions

def grade_exam(exam):
    """批改：逐题判分并统计知识点掌握情况。"""
    results, kp_stat = [], {}
    for q in exam["questions"]:
        ok = is_correct(q.get("student_answer"), q["answer"])
        results.append({"kp": q["kp"], "correct": ok})
        st = kp_stat.setdefault(q["kp"], {"name": q["kp_name"], "total": 0, "wrong": 0})
        st["total"] += 1
        if not ok:
            st["wrong"] += 1
    total = len(results)
    correct = sum(1 for r in results if r["correct"])
    return results, kp_stat, total, correct

def analyze_student(db, sid):
    """汇总该生全部已批改试卷，得到知识点掌握率与薄弱清单。"""
    kp_stat = {}
    exams = [e for e in db["exams"] if e["student_id"] == sid and e.get("graded")]
    for e in exams:
        for q, r in zip(e["questions"], e["results"]):
            st = kp_stat.setdefault(q["kp"], {"kp": q["kp"], "name": q["kp_name"],
                                              "total": 0, "wrong": 0})
            st["total"] += 1
            if not r["correct"]:
                st["wrong"] += 1
                if q.get("analysis") and not st.get("ai_hint"):
                    st["ai_hint"] = q["analysis"]
    for st in kp_stat.values():
        st["rate"] = round((st["total"] - st["wrong"]) / st["total"] * 100, 1)
        st["weak"] = st["rate"] < 80
        if st["kp"] in TEMPLATES:
            st["hint"] = TEMPLATES[st["kp"]]["hint"]
        else:
            st["hint"] = st.pop("ai_hint", None) or "AI 批改识别的薄弱知识点，详见错题分析报告。"
    weak = sorted([s for s in kp_stat.values() if s["weak"]], key=lambda x: x["rate"])
    history = [{"exam_id": e["id"], "title": e["title"], "date": e["date"],
                "score": e["score"], "total_score": e["total_score"]} for e in exams]
    return {"kp_stat": sorted(kp_stat.values(), key=lambda x: x["rate"]),
            "weak": weak, "history": history, "exam_count": len(exams)}

def build_training_local(db, sid, kp_list, count_per_kp):
    """离线模板出题（仅作为 AI 出题失败时的明确降级，界面会标注来源）。
    kp_list 元素可为内置模板 key，或 {"name": 知识点名}。"""
    student = next((s for s in db["students"] if s["id"] == sid), None)
    rng = random.Random()
    items = []
    kp_names = []
    fallback_used = False
    NAME2KP = {v["name"]: k for k, v in TEMPLATES.items()}
    ALIAS = {"拼音": "pinyin", "看拼音写汉字": "pinyin", "字词": "pinyin",
             "生字": "pinyin", "看拼音写字": "pinyin", "汉字书写": "pinyin",
             "形近字": "xingjin", "同音字": "xingjin", "错别字": "xingjin",
             "选字填空": "xingjin", "多音字注音": "duoyin", "注音": "duoyin",
             "古诗默写": "poem", "古诗词默写": "poem", "古诗积累": "poem",
             "课文默写": "poem", "默写": "poem", "日积月累": "poem",
             "成语": "chengyu", "成语填空": "chengyu", "成语补全": "chengyu",
             "病句": "sick", "病句修改": "sick", "修改句子": "sick",
             "修辞": "rhetoric", "修辞判断": "rhetoric", "修辞辨析": "rhetoric",
             "近义字": "synonym", "近义词语": "synonym",
             "反义字": "antonym", "反义词语": "antonym",
             "关联词": "conj", "关联词语": "conj", "关联词填空": "conj",
             "文学常识填空": "literary", "作家作品": "literary", "名著常识": "literary",
             "句式转换": "rewrite", "句子变换": "rewrite", "缩句": "rewrite",
             "扩句": "rewrite", "把字句被字句": "rewrite",
             "文言文": "classical", "文言实词": "classical", "文言虚词": "classical",
             "文言字义": "classical", "古文理解": "classical",
             "标点": "punct", "标点使用": "punct", "加标点": "punct"}
    for entry in kp_list:
        if isinstance(entry, dict):
            name = str(entry.get("name", "")).strip()
            kp = NAME2KP.get(name) or ALIAS.get(name)
            if not kp:
                for k, v in TEMPLATES.items():
                    if v["name"] in name or name in v["name"]:
                        kp = k
                        break
            if not kp:
                kp = rng.choice(["pinyin", "poem", "chengyu", "sick"])
                fallback_used = True
            label = name or TEMPLATES[kp]["name"]
        else:
            kp = entry
            label = TEMPLATES[kp]["name"]
        kp_names.append(label)
        used = set()
        for _ in range(count_per_kp):
            while True:
                q = generate_question(kp, rng.randint(1, 10**9))
                if q["stem"] not in used:
                    used.add(q["stem"])
                    break
            items.append({"stem": q["stem"], "kp_name": label, "answer": q["answer"]})
    training = {
        "id": new_id("tr"),
        "student_id": sid,
        "student_name": student["name"] if student else "学生",
        "grade": student.get("grade", "") if student else "",
        "date": time.strftime("%Y-%m-%d"),
        "title": (student["name"] if student else "学生") + " · 针对性强化训练",
        "kp_names": kp_names,
        "fallback_used": fallback_used,
        "source": "local",
        "items": items,
    }
    db["trainings"].append(training)
    save_db(db)
    return training

def build_training_ai(cfg, db, sid, exam_id, count_per_kp, kp_names=None):
    """AI 出题：把学生错题（原题+作答+错因+知识点）喂给用户的 AI 接口，
    要求生成同类型新题并附参考答案。失败抛 ApiError，由前端决定是否降级。"""
    student = next((s for s in db["students"] if s["id"] == sid), None)
    exam = next((e for e in db["exams"] if e["id"] == exam_id), None) if exam_id else None
    wrongs = []
    if exam and exam.get("graded"):
        for q, r in zip(exam["questions"], exam["results"]):
            if not r["correct"]:
                wrongs.append({"stem": q["stem"], "student_answer": q.get("student_answer", ""),
                               "correct_answer": q.get("answer", ""),
                               "kp_name": q["kp_name"], "analysis": q.get("analysis", "")})
    targets = kp_names or list(dict.fromkeys(w["kp_name"] for w in wrongs))
    if not targets:
        raise ApiError("没有可用于出题的错题或知识点")
    grade = (student.get("grade") if student else "") or "小学"
    prompt = f"""你是一名资深中小学语文教师。学生（{grade}）在以下知识点上出错，请针对每个知识点各出 {count_per_kp} 道同类型的新练习题。
要求：
1. 题目要针对学生的具体错误设计（如他写错某个字，就围绕该字及同类易混字出题），但不得照抄原题；
2. 难度贴合学生年级；题干完整、可独立作答；古诗文、文学常识等内容必须准确无误；
3. 每题附参考答案；
4. 只输出一个 JSON 对象，不要输出其他文字或 Markdown 代码块标记，格式：
{{"items":[{{"stem":"题干","kp_name":"知识点名称","answer":"参考答案"}}]}}

需要出题的知识点：{"、".join(targets)}

学生错题记录（供针对性设计参考）：
{json.dumps(wrongs, ensure_ascii=False, indent=1) if wrongs else "（无具体错题，按知识点常规易错点设计）"}"""
    obj = call_ai_text(cfg, prompt, max_tokens=4096)
    items = obj.get("items")
    if not isinstance(items, list) or not items:
        raise ApiError("AI 未返回有效的训练题列表")
    clean = []
    for it in items:
        stem = str(it.get("stem") or "").strip()
        if not stem:
            continue
        clean.append({"stem": stem,
                      "kp_name": str(it.get("kp_name") or "综合").strip() or "综合",
                      "answer": str(it.get("answer") or "").strip()})
    if not clean:
        raise ApiError("AI 返回的训练题为空")
    training = {
        "id": new_id("tr"), "student_id": sid,
        "student_name": student["name"] if student else "学生",
        "grade": student.get("grade", "") if student else "",
        "date": time.strftime("%Y-%m-%d"),
        "title": (student["name"] if student else "学生") + " · AI 针对性训练",
        "kp_names": list(dict.fromkeys(i["kp_name"] for i in clean)),
        "source": "ai", "exam_id": exam_id,
        "items": clean,
    }
    db["trainings"].append(training)
    save_db(db)
    return training

# ---------------------------------------------------------------------------
# 标准答案库：上传答案图片 -> AI 提取答案文本（一次性），全班批改对照文本
# ---------------------------------------------------------------------------

ANSWER_EXTRACT_PROMPT = """这是一份语文试卷/练习的标准答案（可能含评分说明）。请把答案完整提取为结构化文本，供后续批改对照使用。
要求：
1. 按题号逐题提取标准答案；有多个可接受答案的都列出；主观题提取评分要点；
2. 保留题号与题目简称，便于批改时对齐；
3. 只输出一个 JSON 对象，不要输出其他文字或代码块标记，格式：
{"paper_title": "试卷名称（识别不到写：语文练习）", "answers_text": "1. xxx\\n2. xxx\\n…（完整答案文本）", "question_count": 题目数量}"""

def extract_answer_key(cfg, images):
    """images: [(b64, mime)]；调用视觉模型提取答案文本。"""
    parts = [{"type": "text", "text": ANSWER_EXTRACT_PROMPT}]
    for b64, mime in images:
        parts.append({"type": "image_url",
                      "image_url": {"url": f"data:{mime};base64,{b64}"}})
    reasoning = cfg.get("reasoning", "auto")
    extra = thinking_param(cfg, disable=True) if reasoning == "off" else None
    content = _request_with_param_fallback(
        cfg, [{"role": "user", "content": parts}],
        max_tokens=4096, timeout=240, extra=extra)
    obj = parse_ai_json(content)
    text = str(obj.get("answers_text") or "").strip()
    if not text:
        raise ApiError("AI 未能从图片中提取出答案文本")
    return {"paper_title": str(obj.get("paper_title") or "语文练习").strip(),
            "answers_text": text,
            "question_count": int(obj.get("question_count") or 0)}

# ---------------------------------------------------------------------------
# 分层出题：按学情档案分层制定计划，针对性专项训练
# ---------------------------------------------------------------------------

def tier_plan(db, sid):
    """根据累计学情把知识点分为三层，生成分层计划。
    基础层（掌握率<60%）：字词、默写、句子等基本功专项优先；
    巩固层（60~80%）：错题变式强化；提升层（>=80% 或全达标）：拓展运用。"""
    ana = analyze_student(db, sid)
    stats = ana["kp_stat"]
    BASIC_HINTS = ("拼音", "字词", "词语", "形近", "多音", "默写", "古诗", "成语",
                   "近义", "反义", "标点", "书写", "错别字")
    SENTENCE_HINTS = ("病句", "句子", "句式", "关联", "修辞", "仿写", "扩句", "缩句")
    basic, consol, adv = [], [], []
    for st in stats:
        if st["rate"] < 60:
            basic.append(st)
        elif st["rate"] < 80:
            consol.append(st)
        else:
            adv.append(st)
    def is_basic(st):
        n = st["name"]
        return any(h in n for h in BASIC_HINTS)
    def is_sentence(st):
        n = st["name"]
        return any(h in n for h in SENTENCE_HINTS)
    # 基础层内部排序：字词默写最优先，其次句子，再其他
    basic.sort(key=lambda s: (0 if is_basic(s) else 1 if is_sentence(s) else 2, s["rate"]))
    consol.sort(key=lambda s: s["rate"])
    plan = {
        "exam_count": ana["exam_count"],
        "tiers": [],
        "summary": "",
    }
    if basic:
        plan["tiers"].append({
            "tier": "基础层", "priority": 1,
            "focus": "字词默写与句子基本功专项",
            "reason": "以下知识点掌握率低于60%，属于基础不牢，优先补基本功",
            "kps": [{"name": s["name"], "rate": s["rate"]} for s in basic],
            "count_per_kp": 5,
            "design": "看拼音写词语、形近字组词、古诗课文默写、修改病句、句式仿写等基础题型，题量足、难度低、重复巩固",
        })
    if consol:
        plan["tiers"].append({
            "tier": "巩固层", "priority": 2,
            "focus": "错题变式强化",
            "reason": "掌握率60%~80%，会而不熟，用同类变式题练到稳定",
            "kps": [{"name": s["name"], "rate": s["rate"]} for s in consol],
            "count_per_kp": 4,
            "design": "针对易错点设计同类型不同内容的变式题，中等难度",
        })
    if not basic and not consol:
        plan["tiers"].append({
            "tier": "提升层", "priority": 3,
            "focus": "拓展运用与阅读表达",
            "reason": "基础知识点全部达标，向综合运用提升",
            "kps": [{"name": s["name"], "rate": s["rate"]} for s in adv[:4]],
            "count_per_kp": 3,
            "design": "阅读理解小段、语言运用、小练笔等拓展题，略有挑战",
        })
    elif adv:
        plan["tiers"].append({
            "tier": "提升层", "priority": 3,
            "focus": "已达标知识点少量拓展",
            "reason": "这些知识点已达标，少量拓展保持手感",
            "kps": [{"name": s["name"], "rate": s["rate"]} for s in adv[:3]],
            "count_per_kp": 2,
            "design": "在已掌握知识点上做一点综合运用拓展",
        })
    parts = []
    for t in plan["tiers"]:
        parts.append(f"{t['tier']}（{t['focus']}）：{'、'.join(k['name'] for k in t['kps']) or '综合'}")
    plan["summary"] = "；".join(parts) if parts else "暂无学情数据"
    return plan

def build_tiered_training(cfg, db, sid, count_scale=1.0):
    """按分层计划调用 AI 出题，题目带 tier 标注。"""
    student = next((s for s in db["students"] if s["id"] == sid), None)
    if not student:
        raise ApiError("学生不存在")
    plan = tier_plan(db, sid)
    if not plan["tiers"] or plan["exam_count"] == 0:
        raise ApiError("该生还没有批改记录，先批改一份试卷再分层出题")
    grade = student.get("grade") or "小学"
    prompt = f"""你是一名资深中小学语文教师。请根据下面这份分层训练计划，为学生（{grade}）出题。
分层计划（按优先级执行，基础层最重要）：
{json.dumps(plan["tiers"], ensure_ascii=False, indent=1)}

要求：
1. 每个层级按其 design 描述的题型风格出题，每个知识点出 count_per_kp 指定数量的题；
2. 基础层题目要基础、可重复巩固（字词默写、句子练习优先）；巩固层做变式；提升层略拓展；
3. 题干完整可独立作答，古诗文与文学常识必须准确；每题附参考答案；
4. 只输出一个 JSON 对象，不要输出其他文字或代码块标记，格式：
{{"items":[{{"stem":"题干","kp_name":"知识点","answer":"参考答案","tier":"基础层/巩固层/提升层"}}]}}"""
    obj = call_ai_text(cfg, prompt, max_tokens=6144)
    items = obj.get("items")
    if not isinstance(items, list) or not items:
        raise ApiError("AI 未返回有效的分层训练题")
    order = {"基础层": 0, "巩固层": 1, "提升层": 2}
    clean = []
    for it in items:
        stem = str(it.get("stem") or "").strip()
        if not stem:
            continue
        tier = str(it.get("tier") or "基础层").strip()
        if tier not in order:
            tier = "基础层"
        clean.append({"stem": stem,
                      "kp_name": str(it.get("kp_name") or "综合").strip() or "综合",
                      "answer": str(it.get("answer") or "").strip(),
                      "tier": tier})
    if not clean:
        raise ApiError("AI 返回的分层训练题为空")
    clean.sort(key=lambda x: order[x["tier"]])
    training = {
        "id": new_id("tr"), "student_id": sid,
        "student_name": student["name"],
        "grade": student.get("grade", ""),
        "date": time.strftime("%Y-%m-%d"),
        "title": student["name"] + " · 分层专项训练",
        "kp_names": list(dict.fromkeys(i["kp_name"] for i in clean)),
        "source": "ai", "tiered": True, "plan": plan,
        "items": clean,
    }
    db["trainings"].append(training)
    save_db(db)
    return training

# ---------------------------------------------------------------------------
# PDF 导出：无答案版（纯训练题+留白）与答案版（题目+参考答案）；A4 宋体
# ---------------------------------------------------------------------------

FONT_DIR = "C:/Windows/Fonts"

def build_training_pdf(training, with_answers=False):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, TableStyle)
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_LEFT

    for name, fn in [("SimSun", "simsun.ttc"), ("SimHei", "simhei.ttf")]:
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, os.path.join(FONT_DIR, fn)))

    def xesc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    title = training["title"] + ("（答案版）" if with_answers else "")
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title=title, author="智批语文",
    )
    st_title = ParagraphStyle("t", fontName="SimHei", fontSize=17, leading=24,
                              alignment=TA_CENTER, spaceAfter=4)
    st_info = ParagraphStyle("i", fontName="SimSun", fontSize=10.5, leading=16,
                             alignment=TA_CENTER, textColor=colors.HexColor("#444444"))
    st_body = ParagraphStyle("b", fontName="SimSun", fontSize=12.5, leading=20,
                             alignment=TA_LEFT, firstLineIndent=0)
    st_line = ParagraphStyle("l", fontName="SimSun", fontSize=11, leading=16,
                             textColor=colors.HexColor("#666666"))
    st_ans = ParagraphStyle("a", fontName="SimSun", fontSize=11.5, leading=18,
                            textColor=colors.HexColor("#1e6b3a"), leftIndent=14)

    story = []
    story.append(Paragraph(xesc(title), st_title))
    info = f"{training.get('grade','')}　日期：{training['date']}　共 {len(training['items'])} 题"
    story.append(Paragraph(info, st_info))
    story.append(Spacer(1, 3 * mm))
    rule = Table([[""]], colWidths=[170 * mm], rowHeights=[1])
    rule.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.8,
                               colors.HexColor("#333333"))]))
    story.append(rule)
    story.append(Spacer(1, 5 * mm))

    # 判断/选择/连线类题：题干已含作答方式，无需横线留白
    NO_LINE_PAT = re.compile(r"判断|对的打|错的打|正确.*错误|打[“\"']?[√✓×]|选择|选出|选一选|填序号|连线|连一连|排序|排列顺序")

    st_tier = ParagraphStyle("tier", fontName="SimHei", fontSize=13, leading=19,
                             spaceBefore=4, spaceAfter=3,
                             textColor=colors.HexColor("#333333"))
    last_tier = None
    for idx, item in enumerate(training["items"], 1):
        if training.get("tiered") and item.get("tier") and item["tier"] != last_tier:
            last_tier = item["tier"]
            story.append(Paragraph(f"◆ {xesc(last_tier)}", st_tier))
        story.append(Paragraph(f"{idx}．{xesc(item['stem'])}", st_body))
        if with_answers:
            story.append(Spacer(1, 2 * mm))
            story.append(Paragraph("参考答案：" + xesc(item.get("answer") or "（略）"), st_ans))
            story.append(Spacer(1, 6 * mm))
        elif NO_LINE_PAT.search(item["stem"]):
            story.append(Spacer(1, 8 * mm))
        else:
            story.append(Spacer(1, 7 * mm))
            story.append(Paragraph("答：" + "＿" * 26, st_line))
            story.append(Spacer(1, 9 * mm))

    doc.build(story)
    return buf.getvalue()

# ---------------------------------------------------------------------------
# AI 接口调用（用户自配 OpenAI 兼容多模态接口）与演示数据
# ---------------------------------------------------------------------------

UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

GRADING_PROMPT = """你是一名资深中小学语文教师，请批改这张学生语文试卷（或语文作业）照片。
要求：
1. 逐题识别印刷题干与学生的手写作答，判断对错并给出参考答案；作文/阅读简答等主观题按要点给分，答对主要要点即算对，并在 analysis 中说明扣分点；
2. 对每道错题（含错别字）分析错误原因，并标注知识点名称。知识点请优先从以下列表选择：看拼音写词语、形近字辨析、多音字、古诗文默写、成语积累、修改病句、修辞手法、近义词、反义词、关联词运用、文学常识、句式变换、文言字词理解、标点符号；列表之外的（如阅读理解、作文）可用准确简短的名称；
3. 错别字要具体指出：哪个字写错了、正确写法是什么；默写题要指出错字或漏字；
4. 为每题给出 bbox：该题作答区域在整张图片中的位置，用 0~1 的相对小数坐标 [左上x, 左上y, 右下x, 右下y]（以图片左上角为原点，整图宽=1、高=1；例如 [0.12, 0.30, 0.55, 0.36]；不要输出像素值）。尽量框住学生手写作答的位置；无法确定时给该题题干所在行的大致区域；
5. summary 写整卷总评；parent_report 写给家长看的小结，对象是家长，语言平实不用术语，包含三部分：孩子主要问题是什么（结合具体错题举例）、问题的原因、家长可以怎么做（给出 3~5 条具体可执行的步骤，每条含做法和频率，如"每天晚饭后听写10个词语，错字抄3遍并组词"）。
只输出一个 JSON 对象，不要输出其他任何文字、解释或 Markdown 代码块标记，格式如下：
{
  "paper_title": "试卷名称（识别不到就写：语文练习卷）",
  "student_name": "卷面姓名（识别不到留空字符串）",
  "total_questions": 题目总数,
  "correct_count": 答对题数,
  "score": 百分制得分整数,
  "summary": "整卷总评（60字以内）",
  "parent_report": {
    "problems": "孩子的主要问题（结合错题举例，100字以内）",
    "cause": "问题背后的原因（60字以内）",
    "steps": ["具体步骤1（含做法与频率）", "具体步骤2", "具体步骤3"]
  },
  "questions": [
    {"no": 1, "stem": "题干", "student_answer": "学生作答", "correct_answer": "参考答案",
     "correct": true, "kp_name": "知识点名称", "analysis": "错因分析（答对的题留空字符串）",
     "bbox": [0.1, 0.2, 0.5, 0.26]}
  ]
}
无法辨认的题目：student_answer 填 "?"，correct 填 false，analysis 写 "字迹无法辨认，建议人工复核"。"""

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}

def save_config(cfg):
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CONFIG_FILE)

def mask_key(key):
    if not key:
        return ""
    return key[:6] + "****" + key[-4:] if len(key) > 12 else "****"

def _ai_request(cfg, messages, max_tokens=4096, temperature=0.2, timeout=180, extra=None):
    """向用户配置的 OpenAI 兼容接口发请求，返回 message.content。
    网络抖动/限流/5xx 自动重试 2 次（退避 2s/5s）。"""
    import urllib.request
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base:
        raise ApiError("尚未配置 AI 接口，请先在「AI 接口设置」中填写")
    url = base + ("/chat/completions" if not base.endswith("/chat/completions") else "")
    payload = {
        "model": cfg.get("model") or "gpt-4o",
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if extra:
        payload.update(extra)
    last_err = None
    for attempt, wait in enumerate([0, 2, 5]):
        if wait:
            time.sleep(wait)
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + (cfg.get("api_key") or "")})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as exc:
            detail = str(exc)
            status = getattr(exc, "code", None)
            if hasattr(exc, "read"):
                try:
                    detail = exc.read().decode("utf-8", "replace")[:500]
                except OSError:
                    pass
            last_err = ApiError("AI 接口调用失败：" + detail)
            # 4xx 参数/鉴权类错误不重试（重试也没用），网络类与 429/5xx 重试
            retryable = status is None or status == 429 or (isinstance(status, int) and status >= 500)
            if not retryable or attempt == 2:
                raise last_err
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ApiError("AI 接口返回格式异常：" + json.dumps(data, ensure_ascii=False)[:300])

FAST_SUFFIX = """
【快速模式】直接给出最终 JSON，不要输出思考过程；analysis 每错题一句话点明错因即可（30字以内）；summary 40字以内。注意：每题的 bbox 坐标字段不可省略，必须给出该题作答区域在图片中的大致位置 [左上x,左上y,右下x,右下y]（0~1 相对坐标），parent_report 也不可省略。在保证判分准确与 bbox 完整的前提下尽量精简输出。"""

THINK_SUFFIX = """
【思考模式】请先在内部逐题仔细推敲：核对每个字的书写、默写逐字比对、病句先判病因再核对修改、主观题按要点逐条对照，确认无误后再输出最终 JSON。analysis 要详细：指出具体错在哪里、为什么错、正确思路是什么（每题80字以内）；summary 给出分维度的学习建议（150字以内）。最终仍只输出一个 JSON 对象。"""

def thinking_param(cfg, disable):
    """按接口厂商生成 开启/关闭 模型内置思考的参数。
    disable=True 关闭思考；False 显式开启；返回 None 表示该厂商无已知参数。"""
    base = (cfg.get("base_url") or "").lower()
    model = (cfg.get("model") or "").lower()
    if "volces" in base or "ark" in base or "doubao" in model:
        return {"thinking": {"type": "disabled" if disable else "enabled"}}
    if "dashscope" in base or "qwen" in model:
        return {"enable_thinking": not disable}
    if "bigmodel" in base or "glm" in model:
        return {"thinking": {"type": "disabled" if disable else "enabled"}}
    if "deepseek" in base:
        return None  # deepseek 靠模型名区分 reasoner/chat，无请求参数
    return {"enable_thinking": not disable}  # 其他兼容接口常见参数

def _request_with_param_fallback(cfg, messages, max_tokens, timeout, extra):
    """带思考参数请求；若参数被接口拒绝（400/未知参数），去掉参数自动重试一次。"""
    if not extra:
        return _ai_request(cfg, messages, max_tokens=max_tokens, timeout=timeout)
    try:
        return _ai_request(cfg, messages, max_tokens=max_tokens, timeout=timeout, extra=extra)
    except ApiError as exc:
        msg = str(exc)
        rejected = any(k in msg for k in
                       ("thinking", "InvalidParameter", "invalid_parameter",
                        "unsupported", "unknown", "HTTP Error 400"))
        if rejected:
            return _ai_request(cfg, messages, max_tokens=max_tokens, timeout=timeout)
        raise

def call_ai_grading(cfg, images, mode="fast", custom="", answer_text=""):
    """批改一份试卷（1~N 张图片，多张视为同一份卷子的连续页）。
    images: [(image_b64, mime), ...]；mode=fast/think；custom=用户自定义批改要求；
    answer_text=标准答案文本（有则以答案为准判分，不必重复识别答案图片）。
    cfg.reasoning: auto（默认，不干预）/ off（强制关闭思考）/ on（保持思考）。"""
    prompt = GRADING_PROMPT + (THINK_SUFFIX if mode == "think" else FAST_SUFFIX)
    if answer_text:
        prompt += ("\n【标准答案（教师已提供，判分以此为准；correct_answer 字段直接采用这里的标准答案，"
                   "不要自行另拟答案；按题号或题目内容对齐）】\n" + answer_text)
    custom = (custom or cfg.get("custom_prompt") or "").strip()
    if custom:
        prompt += "\n【教师自定义批改要求（优先遵循，但不得改变 JSON 输出格式）】\n" + custom
    if len(images) > 1:
        prompt += f"\n本次上传了 {len(images)} 张图片，它们是同一份试卷的连续页面，请合并为一份卷子批改，题号连续编排；bbox 坐标相对该题所在的那张图片，并在题目 JSON 中增加 page 字段（从0开始的图片序号）。"
    content_parts = [{"type": "text", "text": prompt}]
    for b64, mime in images:
        content_parts.append({"type": "image_url",
                              "image_url": {"url": f"data:{mime};base64,{b64}"}})
    messages = [{"role": "user", "content": content_parts}]
    reasoning = cfg.get("reasoning", "auto")
    extra = None
    if reasoning == "off":
        extra = thinking_param(cfg, disable=True)
        # 关闭思考的模型容易省略 bbox，提示词再压一遍
        messages[0]["content"][0]["text"] += "\n【重要】即使快速作答，也必须为每一题输出 bbox 坐标与 page 字段，缺失视为无效结果。"
    elif reasoning == "on" or mode == "think":
        extra = thinking_param(cfg, disable=False)
    content = _request_with_param_fallback(
        cfg, messages, max_tokens=6144 if mode == "think" else 4096,
        timeout=300 if mode == "think" else 240, extra=extra)
    result = parse_ai_json(content)
    if not isinstance(result.get("questions"), list) or not result["questions"]:
        raise ApiError("AI 返回缺少 questions 题目列表")
    # 关闭思考后若模型丢了全部 bbox：自动补一次定位（仅当用户选了 off）。
    # 用精简的"只定位"提示词（不要求判分），命中率更高、输出更快。
    need_refill = reasoning == "off" and not any(q.get("bbox") for q in result["questions"])
    if need_refill:
        try:
            stems = [{"no": i + 1, "stem": q.get("stem", "")[:60]}
                     for i, q in enumerate(result["questions"])]
            loc_prompt = ("这是一张学生试卷图片和它的题目清单。请只为每道题定位：输出该题作答区域在图片中的位置。"
                          "只输出 JSON：{\"locations\":[{\"no\":1,\"bbox\":[左上x,左上y,右下x,右下y],\"page\":0}]}，"
                          "坐标为 0~1 相对值，覆盖每道题，不要输出其他内容。\n题目清单：" +
                          json.dumps(stems, ensure_ascii=False))
            loc_parts = [{"type": "text", "text": loc_prompt}] + content_parts[1:]
            content2 = _ai_request(cfg, [{"role": "user", "content": loc_parts}],
                                   max_tokens=2048, timeout=120)
            obj2 = parse_ai_json(content2)
            locs = obj2.get("locations") or obj2.get("questions") or []
            by_no = {}
            for L in locs:
                try:
                    by_no[int(L.get("no", 0))] = L
                except (TypeError, ValueError):
                    continue
            filled = 0
            for i, q in enumerate(result["questions"], 1):
                L = by_no.get(i)
                if L and isinstance(L.get("bbox"), list) and len(L["bbox"]) == 4:
                    q["bbox"] = L["bbox"]
                    q["page"] = L.get("page", 0)
                    filled += 1
            if filled:
                result["_bbox_refilled"] = True
        except (ApiError, KeyError, TypeError, ValueError):
            pass  # 补定位失败不影响批改结果
    return result

def call_ai_text(cfg, prompt, max_tokens=4096):
    """纯文本调用（AI 出题用）。遵循推理模式设置。"""
    reasoning = cfg.get("reasoning", "auto")
    extra = None
    if reasoning == "off":
        extra = thinking_param(cfg, disable=True)
    elif reasoning == "on":
        extra = thinking_param(cfg, disable=False)
    content = _request_with_param_fallback(
        cfg, [{"role": "user", "content": prompt}],
        max_tokens=max_tokens, timeout=240, extra=extra)
    return parse_ai_json(content)

def parse_ai_json(content):
    """容错解析 AI 返回：剥离代码块围栏、截取首个完整 JSON 对象。"""
    if isinstance(content, list):  # 某些接口返回分段 content
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    s = str(content).strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    start = s.find("{")
    if start < 0:
        raise ApiError("AI 未返回 JSON 结果，原始回复：" + s[:300])
    depth, end = 0, -1
    in_str, esc = False, False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        raise ApiError("AI 返回的 JSON 不完整：" + s[:300])
    try:
        obj = json.loads(s[start:end])
    except json.JSONDecodeError as exc:
        raise ApiError("AI 返回 JSON 解析失败：" + str(exc))
    return obj

def normalize_ai_result(obj):
    """把 AI 结果规整为内部 exam 结构，得分与统计以逐题结果重新计算为准。"""
    questions, results = [], []
    kp_stat = {}
    for i, q in enumerate(obj["questions"], 1):
        kp_name = str(q.get("kp_name") or "综合").strip() or "综合"
        ok = bool(q.get("correct"))
        bbox = q.get("bbox")
        if isinstance(bbox, list) and len(bbox) == 4:
            try:
                bbox = [float(v) for v in bbox]
                # 千分比坐标（0~1000）：全部 >1 且 <=1000 时按 /1000 归一
                if all(1.0 < v <= 1000.0 for v in bbox):
                    bbox = [v / 1000.0 for v in bbox]
                # 像素坐标：任一分量 >1 即按图片实际尺寸归一化
                elif any(v > 1.0 for v in bbox):
                    size = q.get("_img_size")
                    if size and size[0] > 0 and size[1] > 0:
                        bbox = [bbox[0] / size[0], bbox[1] / size[1],
                                bbox[2] / size[0], bbox[3] / size[1]]
                    else:
                        bbox = None
                if bbox is not None:
                    bbox = [max(0.0, min(1.0, v)) for v in bbox]
                    # [x, y, w, h] 宽高格式兜底转换
                    if bbox[2] <= bbox[0] and bbox[3] <= bbox[1]:
                        bbox = [bbox[0], bbox[1],
                                min(1.0, bbox[0] + bbox[2]), min(1.0, bbox[1] + bbox[3])]
                    # 退化框（宽或高过小，模型定位失败）：给该题一个最小可见框
                    if bbox[2] - bbox[0] < 0.02:
                        bbox[2] = min(1.0, bbox[0] + 0.35)
                    if bbox[3] - bbox[1] < 0.015:
                        bbox[3] = min(1.0, bbox[1] + 0.04)
                    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                        bbox = None
            except (TypeError, ValueError):
                bbox = None
        else:
            bbox = None
        try:
            page = max(0, int(q.get("page", 0)))
        except (TypeError, ValueError):
            page = 0
        questions.append({
            "kp": "ai:" + kp_name, "kp_name": kp_name, "seed": 0,
            "stem": str(q.get("stem") or f"第{q.get('no', i)}题"),
            "answer": str(q.get("correct_answer") or ""),
            "student_answer": str(q.get("student_answer") or ""),
            "analysis": str(q.get("analysis") or ""),
            "bbox": bbox, "page": page,
        })
        results.append({"kp": "ai:" + kp_name, "correct": ok})
        st = kp_stat.setdefault("ai:" + kp_name, {"name": kp_name, "total": 0, "wrong": 0})
        st["total"] += 1
        if not ok:
            st["wrong"] += 1
    total = len(results)
    correct = sum(1 for r in results if r["correct"])
    score = int(correct / total * 100 + 0.5)
    return questions, results, kp_stat, score

def demo_result():
    """演示数据（虚构）：一份 10 题语文小卷，错 4 题，覆盖四类典型错因。"""
    return {
        "paper_title": "五年级语文·基础知识小测（演示）",
        "student_name": "演示学生",
        "summary": "基础积累整体不错，但字词书写和古诗默写有错别字（“鼓励”写成“鼓厉”、“涯”写成“崖”），病句修改找不到病因。",
        "parent_report": {
            "problems": "孩子这次错了4题：把“徘徊”写成“排徊”、“鼓励”写成“鼓厉”、默写把“天涯”写成“天崖”，都是同音字、形近字混淆；病句修改没找到病因，还改出了新语病。",
            "cause": "认字靠“大概样子”而不是记偏旁表义，背诵只动口不动手，病句缺少系统的病因判断方法。",
            "steps": [
                "每天晚饭后听写10个词语（从错字本里选），写错的字抄3遍并组2个词，第二天再听写一遍",
                "把“徘/排”“励/厉”“涯/崖”做成对比卡片贴在书桌前，让孩子说出偏旁和意思的区别，每周复习2次",
                "每周默写2首古诗，先背再默，默完家长对照课本圈出错字，错字单独重写5遍",
                "每周做3道病句修改，先让孩子说出病因（缺成分/搭配不当/前后矛盾/重复啰嗦）再动笔改",
                "两周后用同类小卷再测一次，对比错题是否减少，及时调整练习重点"
            ]
        },
        "questions": [
            {"no": 1, "stem": "看拼音写词语：pái huái（　　　　）",
             "student_answer": "排徊", "correct_answer": "徘徊",
             "correct": False, "kp_name": "看拼音写词语",
             "analysis": "“徘”写成了同音字“排”。“徘徊”表示来回走动，与行走有关，所以是双人旁（彳），不是提手旁。", "bbox": [0.06, 0.14, 0.48, 0.21]},
            {"no": 2, "stem": "选字填空（厉 / 励）：老师鼓（　）我们大胆发言。",
             "student_answer": "厉", "correct_answer": "励",
             "correct": False, "kp_name": "形近字辨析",
             "analysis": "“鼓励”的“励”是“力”字底，表示劝勉、奋勉；“厉”表示严格、猛烈，如“严厉”“厉害”。两字形近但意义不同。", "bbox": [0.54, 0.14, 0.96, 0.21]},
            {"no": 3, "stem": "古诗默写：海内存知己，（　　　　　　）。（王勃《送杜少府之任蜀州》）",
             "student_answer": "天崖若比邻", "correct_answer": "天涯若比邻",
             "correct": False, "kp_name": "古诗文默写",
             "analysis": "“涯”写成了“崖”。“天涯”指天边，与水边、边际有关，是三点水；“崖”指山崖，是山字头。背得出但写错字，属于同音形近致错。", "bbox": [0.06, 0.30, 0.48, 0.37]},
            {"no": 4, "stem": "修改病句：通过这次活动，使我明白了团结的重要。",
             "student_answer": "通过这次活动，我明白了团结的重要的重要性。", "correct_answer": "这次活动使我明白了团结的重要。",
             "correct": False, "kp_name": "修改病句",
             "analysis": "没有识别出病因：原句“通过……使……”连用导致缺主语，删去其一即可。学生的改法反而新增了“的重要的重要性”重复啰嗦的语病。", "bbox": [0.54, 0.30, 0.96, 0.37]},
            {"no": 5, "stem": "给加点字注音：“重新”中的“重”读（　　）。",
             "student_answer": "chóng", "correct_answer": "chóng",
             "correct": True, "kp_name": "多音字", "analysis": "", "bbox": [0.06, 0.46, 0.48, 0.53]},
            {"no": 6, "stem": "把成语补充完整：守株待（　）",
             "student_answer": "兔", "correct_answer": "兔",
             "correct": True, "kp_name": "成语积累", "analysis": "", "bbox": [0.54, 0.46, 0.96, 0.53]},
            {"no": 7, "stem": "指出下列句子运用的修辞手法：小草从土里探出了脑袋，好奇地张望着。",
             "student_answer": "拟人", "correct_answer": "拟人",
             "correct": True, "kp_name": "修辞手法", "analysis": "", "bbox": [0.06, 0.62, 0.48, 0.69]},
            {"no": 8, "stem": "填上恰当的关联词：（　）困难再大，我们（　）要完成任务。",
             "student_answer": "即使……也……", "correct_answer": "即使……也……",
             "correct": True, "kp_name": "关联词运用", "analysis": "", "bbox": [0.54, 0.62, 0.96, 0.69]},
            {"no": 9, "stem": "《西游记》的作者是谁？",
             "student_answer": "吴承恩", "correct_answer": "吴承恩",
             "correct": True, "kp_name": "文学常识", "analysis": "", "bbox": [0.06, 0.78, 0.48, 0.85]},
            {"no": 10, "stem": "写出反义词：骄傲 ——（　　　）",
             "student_answer": "谦虚", "correct_answer": "谦虚",
             "correct": True, "kp_name": "反义词", "analysis": "", "bbox": [0.54, 0.78, 0.96, 0.85]},
        ],
    }

def ensure_student(db, name, grade=""):
    name = (name or "").strip() or "未命名学生"
    stu = next((s for s in db["students"] if s["name"] == name), None)
    if not stu:
        stu = {"id": new_id("stu"), "name": name, "grade": grade,
               "note": "", "created": time.strftime("%Y-%m-%d")}
        db["students"].append(stu)
    return stu

def store_ai_exam(db, obj, source, student_name=None, image_file=None, image_files=None):
    name = (student_name or obj.get("student_name") or "").strip()
    stu = ensure_student(db, name or "未命名学生")
    questions, results, kp_stat, score = normalize_ai_result(obj)
    files = image_files or ([image_file] if image_file else [])
    n_pages = max(1, len(files))
    for q in questions:
        q["page"] = min(q.get("page", 0), n_pages - 1)
    exam = {"id": new_id("ex"), "student_id": stu["id"],
            "title": str(obj.get("paper_title") or "上传试卷"),
            "date": time.strftime("%Y-%m-%d"), "source": source,
            "image": files[0] if files else None, "images": files,
            "summary": str(obj.get("summary") or ""),
            "parent_report": obj.get("parent_report") or None,
            "questions": questions, "results": results, "graded": True,
            "score": score, "total_score": 100, "kp_stat": kp_stat}
    db["exams"].append(exam)
    save_db(db)
    return exam, stu

# ---------------------------------------------------------------------------
# HTTP 服务与 API
# ---------------------------------------------------------------------------

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".png": "image/png",
        ".svg": "image/svg+xml", ".ico": "image/x-icon", ".pdf": "application/pdf"}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > 128 * 1024 * 1024:
            raise ValueError("请求体过大")
        return json.loads(self.rfile.read(n).decode("utf-8"))

    # ---------------- GET ----------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path == "/" or path == "/index.html":
            self._serve_static("index.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return
        if path == "/api/version":
            self._json({"version": "v8-reasoning", "has_refill": True,
                        "reasoning": load_config().get("reasoning", "auto")})
            return
        if path == "/api/bootstrap":
            with LOCK:
                cfg = load_config()
                self._json({"students": DB["students"],
                            "templates": [{"kp": k, "name": v["name"], "hint": v["hint"]}
                                          for k, v in TEMPLATES.items()],
                            "ai_config": {"base_url": cfg.get("base_url", ""),
                                          "model": cfg.get("model", ""),
                                          "api_key_masked": mask_key(cfg.get("api_key", "")),
                                          "custom_prompt": cfg.get("custom_prompt", ""),
                                          "reasoning": cfg.get("reasoning", "auto"),
                                          "configured": bool(cfg.get("base_url") and cfg.get("api_key"))},
                            "trainings": [{k: t[k] for k in
                                           ("id", "student_id", "student_name", "date",
                                            "title", "kp_names")} | {"count": len(t["items"])}
                                          for t in DB["trainings"]],
                            "answer_keys": [{"id": a["id"], "title": a["title"],
                                             "date": a["date"],
                                             "question_count": a.get("question_count", 0)}
                                            for a in DB.get("answer_keys", [])]})
            return
        if path == "/api/demo":
            obj = demo_result()
            questions, results, kp_stat, score = normalize_ai_result(obj)
            self._json({"demo": True, "paper_title": obj["paper_title"],
                        "student_name": obj["student_name"], "summary": obj["summary"],
                       "parent_report": obj.get("parent_report"),
                        "questions": questions, "results": results,
                        "kp_stat": kp_stat, "score": score})
            return
        if path == "/api/student":
            sid = qs.get("id", [""])[0]
            with LOCK:
                stu = next((s for s in DB["students"] if s["id"] == sid), None)
                if not stu:
                    self._json({"error": "学生不存在"}, 404)
                    return
                analysis = analyze_student(DB, sid)
                exams = [{"id": e["id"], "title": e["title"], "date": e["date"],
                          "score": e.get("score"), "total_score": e.get("total_score"),
                          "graded": e.get("graded", False), "count": len(e["questions"])}
                         for e in DB["exams"] if e["student_id"] == sid]
                self._json({"student": stu, "analysis": analysis, "exams": exams})
            return
        if path == "/api/exam":
            eid = qs.get("id", [""])[0]
            with LOCK:
                e = next((x for x in DB["exams"] if x["id"] == eid), None)
                self._json(e if e else {"error": "试卷不存在"}, 200 if e else 404)
            return
        if path == "/api/training":
            tid = qs.get("id", [""])[0]
            with LOCK:
                t = next((x for x in DB["trainings"] if x["id"] == tid), None)
                self._json(t if t else {"error": "训练不存在"}, 200 if t else 404)
            return
        if path == "/api/answer":
            aid = qs.get("id", [""])[0]
            with LOCK:
                a = next((x for x in DB.get("answer_keys", []) if x["id"] == aid), None)
                self._json(a if a else {"error": "答案不存在"}, 200 if a else 404)
            return
        if path == "/api/tierplan":
            sid = qs.get("student_id", [""])[0]
            with LOCK:
                if not any(s["id"] == sid for s in DB["students"]):
                    self._json({"error": "学生不存在"}, 404)
                    return
                self._json({"plan": tier_plan(DB, sid)})
            return
        if path.startswith("/api/training/") and path.endswith("/pdf"):
            tid = path.split("/")[3]
            with_answers = qs.get("answers", ["0"])[0] == "1"
            with LOCK:
                t = next((x for x in DB["trainings"] if x["id"] == tid), None)
            if not t:
                self._json({"error": "训练不存在"}, 404)
                return
            try:
                pdf = build_training_pdf(t, with_answers=with_answers)
            except Exception as exc:  # 字体缺失等
                self._json({"error": "PDF 生成失败：" + str(exc)}, 500)
                return
            fname = (t["title"].replace(" ", "")
                     + ("答案版" if with_answers else "无答案版")
                     + "-" + t["date"] + ".pdf")
            from urllib.parse import quote
            self._send(200, pdf, "application/pdf", {
                "Content-Disposition":
                    f"attachment; filename*=UTF-8''{quote(fname)}",
                "X-Filename": quote(fname),
            })
            return
        if path.startswith("/uploads/"):
            rel = unquote(path[len("/uploads/"):]).replace("\\", "/")
            full = os.path.normpath(os.path.join(UPLOAD_DIR, rel))
            if full.startswith(UPLOAD_DIR) and os.path.isfile(full):
                ext = os.path.splitext(full)[1].lower()
                with open(full, "rb") as f:
                    self._send(200, f.read(), MIME.get(ext, "application/octet-stream"))
            else:
                self._send(404, "not found", "text/plain; charset=utf-8")
            return
        self._json({"error": "not found"}, 404)

    def _serve_static(self, rel):
        rel = unquote(rel).replace("\\", "/")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            self._send(200, f.read(), MIME.get(ext, "application/octet-stream"))

    # ---------------- POST ----------------
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_body()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._json({"error": "请求体格式错误或过大"}, 400)
            return
        try:
            self._post_inner(path, body)
        except ApiError as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:  # 兜底：任何未预期错误都返回 JSON 而非断连
            import traceback
            traceback.print_exc()
            try:
                self._json({"error": "服务器内部错误：" + str(exc)}, 500)
            except OSError:
                pass

    def _post_inner(self, path, body):
        # AI 调用耗时长，不占用数据锁；仅数据读写时加锁
        if path == "/api/grade/upload":
            self._handle_upload_grading(body)
            return
        if path == "/api/ai/test":
            self._handle_ai_test(body)
            return
        if path == "/api/answer/create":
            self._handle_answer_create(body)
            return
        if path == "/api/training/tiered":
            self._handle_tiered_training(body)
            return
        if path == "/api/training/create":
            self._handle_training_create(body)
            return
        with LOCK:
            self._route_post(path, body)

    def _handle_ai_test(self, body):
        import urllib.request
        cfg = load_config()
        base = (body.get("base_url") or cfg.get("base_url") or "").rstrip("/")
        key = body.get("api_key") or cfg.get("api_key") or ""
        model = body.get("model") or cfg.get("model") or "gpt-4o"
        if not base or not key:
            raise ApiError("请先填写接口地址与 API Key")
        url = base + ("/chat/completions" if not base.endswith("/chat/completions") else "")
        payload = {"model": model, "max_tokens": 512,
                   "messages": [{"role": "user", "content": "回复：连接成功"}]}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            reply = data["choices"][0]["message"]["content"]
            self._json({"ok": True, "reply": str(reply)[:60]})
        except Exception as exc:
            detail = str(exc)
            if hasattr(exc, "read"):
                try:
                    detail = exc.read().decode("utf-8", "replace")[:300]
                except OSError:
                    pass
            raise ApiError("连接失败：" + detail)

    def _handle_answer_create(self, body):
        """上传标准答案图片 -> AI 提取答案文本 -> 入库（一次提取，全班复用）。"""
        import base64 as b64mod
        data_urls = body.get("images") or ([body["image"]] if body.get("image") else [])
        if not data_urls:
            raise ApiError("请先上传标准答案图片")
        if len(data_urls) > 4:
            raise ApiError("答案最多 4 张图片")
        images = []
        for data_url in data_urls:
            m = re.match(r"^data:(image/(?:png|jpeg|jpg|webp));base64,(.+)$", data_url, re.S)
            if not m:
                raise ApiError("请上传 PNG / JPG / WEBP 格式的答案图片")
            mime, b64 = m.group(1), m.group(2)
            if mime == "image/jpg":
                mime = "image/jpeg"
            try:
                raw = b64mod.b64decode(b64, validate=True)
            except (ValueError, b64mod.binascii.Error):
                raise ApiError("图片数据无效")
            if len(raw) > 15 * 1024 * 1024:
                raise ApiError("单张图片超过 15MB")
            images.append((b64, mime))
        cfg = load_config()
        if not cfg.get("base_url") or not cfg.get("api_key"):
            raise ApiError("提取答案需要先配置 AI 接口")
        extracted = extract_answer_key(cfg, images)
        title = str(body.get("title") or "").strip() or extracted["paper_title"]
        with LOCK:
            ak = {"id": new_id("ak"), "title": title,
                  "date": time.strftime("%Y-%m-%d"),
                  "answers_text": extracted["answers_text"],
                  "question_count": extracted["question_count"]}
            DB.setdefault("answer_keys", []).append(ak)
            save_db(DB)
        self._json({"answer_key": ak})

    def _handle_tiered_training(self, body):
        with LOCK:
            sid = body.get("student_id")
            if not any(s["id"] == sid for s in DB["students"]):
                raise ApiError("请先选择学生")
            plan_preview = tier_plan(DB, sid)
        if body.get("preview"):
            self._json({"plan": plan_preview})
            return
        cfg = load_config()
        if not cfg.get("base_url") or not cfg.get("api_key"):
            raise ApiError("分层出题需要先配置 AI 接口")
        with LOCK:
            training = build_tiered_training(cfg, DB, sid)
        self._json({"training": training, "plan": training["plan"]})

    def _handle_upload_grading(self, body):
        import base64 as b64mod
        data_urls = body.get("images") or ([body["image"]] if body.get("image") else [])
        if not data_urls:
            raise ApiError("请先上传试卷照片")
        if len(data_urls) > 6:
            raise ApiError("一次最多上传 6 张图片（同一份卷子的连续页）")
        images, raws = [], []
        for data_url in data_urls:
            m = re.match(r"^data:(image/(?:png|jpeg|jpg|webp));base64,(.+)$", data_url, re.S)
            if not m:
                raise ApiError("请上传 PNG / JPG / WEBP 格式的试卷照片")
            mime, image_b64 = m.group(1), m.group(2)
            if mime == "image/jpg":
                mime = "image/jpeg"
            try:
                raw = b64mod.b64decode(image_b64, validate=True)
            except (ValueError, b64mod.binascii.Error):
                raise ApiError("图片数据无效")
            if len(raw) > 15 * 1024 * 1024:
                raise ApiError("单张图片超过 15MB，请压缩后再上传")
            images.append((image_b64, mime))
            raws.append((raw, mime))
        cfg = load_config()
        mode = "think" if body.get("mode") == "think" else "fast"
        custom = str(body.get("custom") or "").strip()
        answer_text = ""
        ak_id = body.get("answer_key_id")
        if ak_id:
            with LOCK:
                ak = next((a for a in DB.get("answer_keys", []) if a["id"] == ak_id), None)
            if not ak:
                raise ApiError("所选标准答案不存在，请重新选择")
            answer_text = ak["answers_text"]
        obj = call_ai_grading(cfg, images, mode=mode, custom=custom,
                              answer_text=answer_text)  # 网络调用不加锁
        # 读取每页图片实际尺寸，供像素坐标归一化（模型可能按像素输出 bbox）
        try:
            from PIL import Image
            sizes = []
            for raw, _mime in raws:
                with Image.open(io.BytesIO(raw)) as im:
                    sizes.append(im.size)  # (w, h)
            for q in obj.get("questions", []):
                try:
                    pg = max(0, int(q.get("page", 0)))
                except (TypeError, ValueError):
                    pg = 0
                if pg < len(sizes):
                    q["_img_size"] = sizes[pg]
        except Exception:
            pass  # PIL 不可用等情况：像素坐标将被丢弃，白板用题号条兜底
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        files = []
        for raw, mime in raws:
            ext = ".jpg" if mime == "image/jpeg" else ("." + mime.split("/")[1])
            fname = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6] + ext
            with open(os.path.join(UPLOAD_DIR, fname), "wb") as f:
                f.write(raw)
            files.append(fname)
        with LOCK:
            exam, stu = store_ai_exam(DB, obj, "ai_upload",
                                      student_name=body.get("student_name"),
                                      image_files=files)
            exam["mode"] = mode
            if custom:
                exam["custom"] = custom
            if ak_id:
                exam["answer_key_id"] = ak_id
                exam["answer_key_title"] = ak["title"]
            save_db(DB)
            analysis = analyze_student(DB, stu["id"])
        self._json({"exam": exam, "student": stu, "analysis": analysis})

    def _handle_training_create(self, body):
        """AI 出题为主；engine=local 时才用离线模板（明确降级）。"""
        with LOCK:
            sid = body.get("student_id")
            if not any(s["id"] == sid for s in DB["students"]):
                raise ApiError("请先选择学生")
            exam_id = body.get("exam_id")
            kp_names = [str(n).strip() for n in (body.get("kp_names") or []) if str(n).strip()]
            per = max(1, min(10, int(body.get("count_per_kp", 4))))
            engine = body.get("engine", "ai")
        if engine == "local":
            with LOCK:
                if not kp_names:
                    ana = analyze_student(DB, sid)
                    kp_names = [w["name"] for w in ana["weak"]]
                training = build_training_local(
                    DB, sid, [{"name": n} for n in kp_names], per)
            self._json({"training": training})
            return
        cfg = load_config()
        if not cfg.get("base_url") or not cfg.get("api_key"):
            raise ApiError("AI 出题需要先在「AI 接口设置」配置接口")
        with LOCK:
            training = build_training_ai(cfg, DB, sid, exam_id, per, kp_names or None)
        self._json({"training": training})

    def _route_post(self, path, body):
        if path == "/api/answer/update":
            aid = body.get("id")
            ak = next((a for a in DB.get("answer_keys", []) if a["id"] == aid), None)
            if not ak:
                raise ApiError("答案不存在")
            if "answers_text" in body:
                text = str(body["answers_text"]).strip()
                if not text:
                    raise ApiError("答案文本不能为空")
                ak["answers_text"] = text[:20000]
                ak["edited"] = True
                ak["edited_at"] = time.strftime("%Y-%m-%d %H:%M")
                # 题数按行首题号粗略重计（仅展示用）
                nums = re.findall(r"^\s*(\d+)[\.、．]", text, re.M)
                if nums:
                    ak["question_count"] = len(set(nums))
            if "title" in body:
                t = str(body["title"]).strip()
                if t:
                    ak["title"] = t[:40]
            save_db(DB)
            self._json({"answer_key": ak})
            return
        if path == "/api/answer/manual":
            title = str(body.get("title") or "").strip()
            text = str(body.get("answers_text") or "").strip()
            if not title:
                raise ApiError("请填写答案名称")
            if not text:
                raise ApiError("请填写答案文本")
            nums = re.findall(r"^\s*(\d+)[\.、．]", text, re.M)
            ak = {"id": new_id("ak"), "title": title[:40],
                  "date": time.strftime("%Y-%m-%d"),
                  "answers_text": text[:20000],
                  "question_count": len(set(nums)) if nums else 0,
                  "manual": True}
            DB.setdefault("answer_keys", []).append(ak)
            save_db(DB)
            self._json({"answer_key": ak})
            return
        if path == "/api/ai/config":
            cfg = load_config()
            # 只更新提交的字段；未提交的字段保留（记忆功能：配置跨更新持久化）
            if "base_url" in body:
                base = str(body["base_url"]).strip().rstrip("/")
                if base and not re.match(r"^https?://", base):
                    raise ApiError("接口地址需以 http:// 或 https:// 开头")
                cfg["base_url"] = base
            if "model" in body:
                cfg["model"] = str(body["model"]).strip() or "gpt-4o"
            if "api_key" in body:
                key = str(body["api_key"]).strip()
                if key:  # 留空表示沿用已保存的 Key
                    cfg["api_key"] = key
            if "custom_prompt" in body:
                cfg["custom_prompt"] = str(body["custom_prompt"]).strip()[:2000]
            if "reasoning" in body:
                r = str(body["reasoning"]).strip()
                cfg["reasoning"] = r if r in ("auto", "off", "on") else "auto"
            save_config(cfg)
            self._json({"configured": bool(cfg.get("base_url") and cfg.get("api_key")),
                        "api_key_masked": mask_key(cfg.get("api_key", "")),
                        "custom_prompt": cfg.get("custom_prompt", ""),
                        "reasoning": cfg.get("reasoning", "auto")})
            return
        if path == "/api/demo/save":
            obj = demo_result()
            exam, stu = store_ai_exam(DB, obj, "demo", student_name="演示学生")
            analysis = analyze_student(DB, stu["id"])
            self._json({"exam": exam, "student": stu, "analysis": analysis})
            return
        if path == "/api/exam/marks":
            # 手动批注（红笔勾/叉/圈/自由笔画），按页存储归一化坐标
            eid = body.get("exam_id")
            exam = next((e for e in DB["exams"] if e["id"] == eid), None)
            if not exam:
                raise ApiError("试卷不存在")
            marks = exam.setdefault("marks", {})
            page = str(int(body.get("page", 0)))
            if "set" in body:      # 整页替换
                items = body["set"]
                if not isinstance(items, list) or len(items) > 500:
                    raise ApiError("批注数据无效")
                marks[page] = items
            if "add" in body:      # 追加一笔
                marks.setdefault(page, []).append(body["add"])
            save_db(DB)
            self._json({"marks": marks})
            return
        if path == "/api/exam/import":
            # 学生/教师人工核对 AI 结果后修正单题判定
            eid = body.get("exam_id")
            exam = next((e for e in DB["exams"] if e["id"] == eid), None)
            if not exam or not exam.get("graded"):
                raise ApiError("试卷不存在或未批改")
            fixes = body.get("fixes") or {}
            for i_str, ok in fixes.items():
                i = int(i_str)
                if 0 <= i < len(exam["results"]):
                    exam["results"][i]["correct"] = bool(ok)
            kp_stat = {}
            for q, r in zip(exam["questions"], exam["results"]):
                st = kp_stat.setdefault(q["kp"], {"name": q["kp_name"], "total": 0, "wrong": 0})
                st["total"] += 1
                if not r["correct"]:
                    st["wrong"] += 1
            correct = sum(1 for r in exam["results"] if r["correct"])
            exam["kp_stat"] = kp_stat
            exam["score"] = int(correct / len(exam["results"]) * 100 + 0.5)
            save_db(DB)
            self._json({"exam": exam,
                        "analysis": analyze_student(DB, exam["student_id"])})
            return
        if path == "/api/student":
            name = str(body.get("name", "")).strip()
            if not name:
                raise ApiError("请填写学生姓名")
            if any(s["name"] == name for s in DB["students"]):
                raise ApiError("已存在同名学生")
            stu = {"id": new_id("stu"), "name": name,
                   "grade": str(body.get("grade", "")).strip(),
                   "note": str(body.get("note", "")).strip(),
                   "created": time.strftime("%Y-%m-%d")}
            DB["students"].append(stu)
            save_db(DB)
            self._json({"student": stu})
            return
        if path == "/api/exam/create":
            sid = body.get("student_id")
            if not any(s["id"] == sid for s in DB["students"]):
                raise ApiError("请先选择学生")
            kps = body.get("kp_list") or []
            kps = [k for k in kps if k in TEMPLATES]
            if not kps:
                raise ApiError("请至少选择一个知识点")
            per = int(body.get("count_per_kp", 2))
            per = max(1, min(6, per))
            title = str(body.get("title", "")).strip() or \
                time.strftime("%m月%d日") + "语文练习卷"
            questions = build_paper(kps, per, int(body.get("seed", int(time.time()))))
            exam = {"id": new_id("ex"), "student_id": sid, "title": title,
                    "date": time.strftime("%Y-%m-%d"),
                    "questions": [{k: q[k] for k in ("kp", "kp_name", "seed", "stem", "answer")}
                                  | {"student_answer": ""} for q in questions],
                    "graded": False}
            DB["exams"].append(exam)
            save_db(DB)
            self._json({"exam": exam})
            return
        if path == "/api/exam/grade":
            eid = body.get("exam_id")
            exam = next((e for e in DB["exams"] if e["id"] == eid), None)
            if not exam:
                raise ApiError("试卷不存在")
            answers = body.get("answers") or {}
            for i, q in enumerate(exam["questions"]):
                q["student_answer"] = str(answers.get(str(i), "")).strip()
            results, kp_stat, total, correct = grade_exam(exam)
            exam["results"] = results
            exam["graded"] = True
            exam["score"] = int(correct / total * 100 + 0.5)
            exam["total_score"] = 100
            exam["kp_stat"] = kp_stat
            save_db(DB)
            analysis = analyze_student(DB, exam["student_id"])
            self._json({"exam": exam, "analysis": analysis})
            return
        if path == "/api/exam/edit":
            # 编辑区保存：修改题干/学生作答/参考答案/知识点/判定/错因，重算得分
            eid = body.get("exam_id")
            exam = next((e for e in DB["exams"] if e["id"] == eid), None)
            if not exam or not exam.get("graded"):
                raise ApiError("试卷不存在或未批改")
            edits = body.get("questions") or []
            for ed in edits:
                i = int(ed.get("index", -1))
                if not (0 <= i < len(exam["questions"])):
                    continue
                q, r = exam["questions"][i], exam["results"][i]
                for field in ("stem", "student_answer", "answer", "kp_name", "analysis"):
                    if field in ed:
                        q[field] = str(ed[field])
                if "kp_name" in ed:
                    q["kp"] = "ai:" + q["kp_name"]
                    r["kp"] = q["kp"]
                if "correct" in ed:
                    r["correct"] = bool(ed["correct"])
                if "bbox" in ed:
                    bb = ed["bbox"]
                    if bb is None:
                        q["bbox"] = None
                    elif isinstance(bb, list) and len(bb) == 4:
                        try:
                            bb = [max(0.0, min(1.0, float(v))) for v in bb]
                            if bb[2] > bb[0] and bb[3] > bb[1]:
                                q["bbox"] = bb
                        except (TypeError, ValueError):
                            pass
                if "page" in ed:
                    try:
                        n_pages = max(1, len(exam.get("images") or
                                           ([exam["image"]] if exam.get("image") else [])))
                        q["page"] = max(0, min(n_pages - 1, int(ed["page"])))
                    except (TypeError, ValueError):
                        pass
            if "title" in body:
                exam["title"] = str(body["title"]).strip() or exam["title"]
            if "summary" in body:
                exam["summary"] = str(body["summary"])
            kp_stat = {}
            for q, r in zip(exam["questions"], exam["results"]):
                st = kp_stat.setdefault(q["kp"], {"name": q["kp_name"], "total": 0, "wrong": 0})
                st["total"] += 1
                if not r["correct"]:
                    st["wrong"] += 1
            correct = sum(1 for r in exam["results"] if r["correct"])
            exam["kp_stat"] = kp_stat
            exam["score"] = int(correct / len(exam["results"]) * 100 + 0.5)
            save_db(DB)
            self._json({"exam": exam,
                        "analysis": analyze_student(DB, exam["student_id"])})
            return
        if path == "/api/student/update":
            sid = body.get("id")
            stu = next((s for s in DB["students"] if s["id"] == sid), None)
            if not stu:
                raise ApiError("学生不存在")
            name = str(body.get("name", stu["name"])).strip()
            if not name:
                raise ApiError("姓名不能为空")
            if any(s["name"] == name and s["id"] != sid for s in DB["students"]):
                raise ApiError("已存在同名学生")
            stu["name"] = name
            stu["grade"] = str(body.get("grade", stu.get("grade", ""))).strip()
            stu["note"] = str(body.get("note", stu.get("note", ""))).strip()
            for t in DB["trainings"]:
                if t["student_id"] == sid:
                    t["student_name"] = name
            save_db(DB)
            self._json({"student": stu})
            return
        self._json({"error": "not found"}, 404)

    # ---------------- DELETE ----------------
    def do_DELETE(self):
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        with LOCK:
            if len(parts) == 3 and parts[0] == "api":
                kind, oid = parts[1], parts[2]
                coll = {"student": "students", "exam": "exams",
                        "training": "trainings", "answer": "answer_keys"}.get(kind)
                if coll:
                    before = len(DB.get(coll, []))
                    DB[coll] = [x for x in DB.get(coll, []) if x["id"] != oid]
                    if kind == "student":
                        DB["exams"] = [e for e in DB["exams"] if e["student_id"] != oid]
                        DB["trainings"] = [t for t in DB["trainings"] if t["student_id"] != oid]
                    save_db(DB)
                    self._json({"deleted": before - len(DB[coll])})
                    return
        self._json({"error": "not found"}, 404)

class ApiError(Exception):
    pass

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    # 单实例锁：同一数据目录只允许一个服务进程，防止旧进程占端口导致“改了没生效”
    lock_file = os.path.join(BASE_DIR, ".server.lock")
    if os.path.exists(lock_file):
        try:
            with open(lock_file, "r", encoding="utf-8") as f:
                old_pid = int(f.read().strip() or 0)
            if old_pid and old_pid != os.getpid():
                import ctypes
                handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, old_pid)
                if handle:
                    ctypes.windll.kernel32.CloseHandle(handle)
                    print(f"检测到旧服务进程 PID={old_pid} 仍在运行，正在替换…")
                    os.system(f'taskkill /PID {old_pid} /F >nul 2>&1')
                    time.sleep(0.8)
        except (ValueError, OSError, AttributeError):
            pass
    with open(lock_file, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        print(f"端口 {args.port} 被占用：{exc}")
        print("请先关闭占用该端口的程序，或换一个端口：python app.py --port 8766")
        raise SystemExit(1)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"智批语文已启动：{url}  （Ctrl+C 退出）")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("已退出。")

if __name__ == "__main__":
    main()