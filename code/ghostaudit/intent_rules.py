"""Keyword baseline for request intent: does the text authorize deletion?

Deterministic, six languages (en, pt, es, fr, ja, ko). The order of the checks is the rule:
1. an explicit refusal of deletion ("I am not asking for deletion", "do not close the account") wins;
2. otherwise deletion wording, or a request to verify or audit a deletion, authorizes deletion;
3. otherwise the text is a non-deletion request (marketing opt-out, access, export, correction, ...).
"""

from __future__ import annotations

import re
import unicodedata

from .models import IntentResult

SOURCE = "rules"


def _rx(*parts: str) -> re.Pattern:
    return re.compile("|".join(f"(?:{p})" for p in parts), re.IGNORECASE)


_DEL_VERB = r"delete|erase|remove|close|deactivate|wipe|purge"
# A refusal must cover the whole account or all data; "do not remove my order history" only limits scope.
_WHOLE = (r"(?:\s+or\s+\w+)?(?:\s*[.!?,;]|\s*$|\s+(?:anything|it|any\s+of\s+my\s+\w+|"
          r"(?:the|my|this)\s+(?:account|profile|data|records|personal\s+data)\b))")

# Explicit refusals of deletion, in every supported language.
NEGATION = _rx(
    # en
    r"\bnot\s+(?:asking|requesting|seeking|looking)\s+(?:for\s+)?(?:an?\s+|the\s+|any\s+)?(?:account\s+)?"
    r"(?:deletion|erasure|removal|closure|to\s+(?:" + _DEL_VERB + r"))",
    r"\bnot\s+an?\s+(?:deletion|erasure|removal)\s+request",
    r"\bnot\s+a\s+request\s+(?:to|for)\s+(?:" + _DEL_VERB + r"|deletion|erasure)",
    r"\b(?:do\s+not|don['’]t|dont|never)\s+(?:\w+\s+){0,2}?(?:" + _DEL_VERB + r")\b" + _WHOLE,
    r"\bwithout\s+(?:deleting|erasing|removing|closing)\b" + _WHOLE,
    r"\b(?:keep|leave)\s+(?:the|my|this)\s+(?:account|profile)\b[^.!?]*?"
    r"\b(?:as\s+it\s+is|as\s+is|active|open|in\s+place|unchanged)\b",
    r"\b(?:continue|keep)\s+using\s+(?:the|this|my)\s+(?:service|account)\b",
    r"\bstill\s+(?:need|use)\s+(?:the|this|my|it)\b",
    # pt
    r"\bnão\s+(?:\w+\s+){0,3}?(?:excluir|exclua|apagar|apague|eliminar|elimine|remover|remova|encerrar|encerre|"
    r"exclusão|eliminação|remoção|encerramento)\b",
    r"\bsem\s+(?:excluir|apagar|eliminar|remover|encerrar)\b",
    # es ("no" alone is also Portuguese "in the", so a verb must follow directly)
    r"\bno\s+(?:quiero|deseo|solicito|pido|estoy\s+pidiendo|eliminen|elimine|borren|borre|cierren|cierre|"
    r"supriman)\b",
    r"\bsin\s+(?:eliminar|borrar|suprimir|cerrar)\b",
    # fr
    r"\bne\s+(?:\w+\s+){0,2}?pas\b[^.!?]{0,40}?(?:supprim|suppression|effac|ferm|clôtur|clotur)",
    r"\bn['’]\w+\s+pas\b[^.!?]{0,40}?(?:supprim|suppression|effac|ferm|clôtur|clotur)",
    r"\bsans\s+(?:supprimer|effacer|fermer|clôturer)\b",
    # ja
    r"(?:削除|消去|退会|解約)(?:は|を)?(?:しないで|しない|不要|希望しません|求めていません|ではありません|ではなく|しません|しなくて)",
    # ko
    r"(?:삭제|탈퇴|파기)[^.!?。]{0,10}?(?:하지\s*마|하지\s*말|원하지\s*않|아닙|아니라|않습니다|않아)",
)

# Deletion wording. Closing counts only next to an account or profile word in the same sentence.
DELETION = _rx(
    # en
    r"\b(?:delet\w*|eras(?:e|ed|es|ing|ure)|remov\w*|wip(?:e|ed|ing)|purg\w*)\b",
    r"\bforget\s+me\b|\bright\s+to\s+be\s+forgotten\b",
    r"\b(?:account|profile)\b[^.!?]*\bclos(?:e|ed|ing|ure)\b|\bclos(?:e|ed|ing|ure)\b[^.!?]*\b(?:account|profile)\b",
    # pt
    r"\b(?:exclu(?:ir|são|sao|am|a|ída|ido)|apag(?:ar|uem|ue|ado|ados)|encerr(?:ar|amento|em)|remo(?:ver|vam|ção|cao))\b",
    # es (and the shared pt/es eliminar)
    r"\b(?:elimin(?:ar|ación|acion|ação|acao|en|e|ado|ada)|borr(?:ar|en|e|ado|ada)|supr(?:imir|iman|esión|esion))\b",
    r"\bcerr(?:ar|ada|ado)\b[^.!?]*\bcuenta\b|\bdar\s+de\s+baja\s+(?:mi|la)\s+cuenta\b",
    # fr
    r"\b(?:supprim\w*|suppression|effac\w*|clôtur\w*|clotur\w*)\b|\bferm(?:er|ez|eture)\b[^.!?]*\bcompte\b",
    r"droit\s+à\s+l['’]oubli",
    # ja / ko
    r"削除|消去|抹消|退会",
    r"삭제|탈퇴|파기|지워",
)

# A request to verify or audit a deletion counts as deletion (the samples treat it so).
VERIFICATION = _rx(
    r"\b(?:verify|confirm|check|audit|inspect|investigate|tell\s+me\s+whether)\b[^.!?]*"
    r"\b(?:deleted|deletion|removed|erased|gone|closed|no\s+longer\s+exists?)\b",
    r"\baudit\s+the\s+deletion\b",
)
IMPERATIVE = _rx(
    r"\b(?:please|kindly|can\s+you|could\s+you|i\s+want\s+to|i\s+would\s+like\s+to)\s+(?:\w+\s+){0,2}?"
    r"(?:" + _DEL_VERB + r"|erase|finish|complete)\b",
    r"\b(?:request|requesting|asking|want|would\s+like)\b[^.!?]*\b(?:delet|eras|remov|clos)\w*",
    r"(?:solicito|demande|依頼|요청)",
)

# Non-deletion request types, checked in order; the first match names the type.
TOPICS = (
    ("marketing_opt_out", _rx(
        r"\b(?:promotional|marketing|newsletters?|unsubscribe|opt[\s-]?out|advertis\w*)\b",
        r"\b(?:publicidad|promocion\w*|publicité|prospection|promocionais|propaganda)\b",
        r"広告|マーケティング|メルマガ|광고|마케팅|수신\s*거부")),
    ("export", _rx(
        r"\bdownload\b|\bportable\b|\bportability\b|\bexport\s+(?:my|the)\s+(?:data|information)\b",
        r"\bportabili(?:dad|dade|té)\b|ダウンロード|エクスポート|다운로드|내보내")),
    ("access", _rx(
        r"\bwhat\s+(?:personal\s+)?(?:information|data)\s+(?:you|do\s+you)\s+hold\b",
        r"\bcopy\s+of\s+(?:the|my|all)\s+(?:personal\s+)?(?:information|data)\b",
        r"\binformation\s+request\b|\baccess\s+(?:to\s+)?my\s+(?:personal\s+)?data\b",
        r"\bacceso\s+a\s+mis\s+datos\b|\bacesso\s+aos\s+meus\s+dados\b|\baccès\s+à\s+mes\s+données\b",
        r"開示|열람")),
    ("correction", _rx(
        r"\bmisspel\w*|\bcorrect\s+(?:it|my|the)\b|\bout\s+of\s+date\b|\bupdate\s+(?:my|the)\b|\breplace\s+it\b",
        r"\bchange\s+(?:the|my)\s+(?:email|e-mail|phone|mobile|name|address)\b",
        r"\brectif\w*|\bcorrigir\b|\bcorregir\b|訂正|수정|정정")),
    ("account_help", _rx(
        r"\breset\w*\s+(?:the\s+|my\s+)?(?:login|password)\b|\bcan['’]?t\s+access\b|\bcannot\s+access\b",
        r"\bunable\s+to\s+(?:log|sign)\s*in\b|\blocked\s+out\b|\bforgot\s+(?:my\s+)?password\b",
        r"ログイン|비밀번호|로그인")),
    ("other", _rx(r"\binvoices?\b|\breceipts?\b|\bfacturas?\b|\bfaturas?\b|\bfactures?\b|請求書|영수증")),
)

_SENTENCE = re.compile(r"(?<=[.!?。？！])\s*")
_MARKETING = TOPICS[0][1]

# Language cues for Latin-script text; the language with the most cues wins, English by default.
_LANG_CUES = (
    ("pt", re.compile(r"\b(?:não|conta|exclusão|verifiquem|confirmem|análise|associada|você|obrigad\w*)\b", re.IGNORECASE)),
    ("es", re.compile(r"\b(?:cuenta|eliminación|correo|revisen|confirmen|revisión|política|pueden|todavía)\b", re.IGNORECASE)),
    ("fr", re.compile(r"\b(?:compte|suppression|veuillez|merci|adresse|votre|vous|définitive|je)\b", re.IGNORECASE)),
)


def detect_language(text: str) -> str:
    """ISO 639-1 code from script (ko, ja) or Latin keyword cues (pt, es, fr), else en."""
    if re.search(r"[가-힯ᄀ-ᇿ]", text):
        return "ko"
    if re.search(r"[぀-ヿ一-鿿]", text):
        return "ja"
    scores = [(len(rx.findall(text)), lang) for lang, rx in _LANG_CUES]
    scores.sort(key=lambda s: -s[0])
    return scores[0][1] if scores[0][0] > scores[1][0] else "en"


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).replace("’", "'")


def _first(rx: re.Pattern, text: str) -> str:
    m = rx.search(text)
    return m.group(0).strip() if m else ""


def _topic(text: str) -> str:
    return next((name for name, rx in TOPICS if rx.search(text)), "other")


def classify(text: str) -> IntentResult:
    """Keyword verdict on whether `text` authorizes deletion (source="rules")."""
    t = _norm(text)
    lang = detect_language(t)
    refusal = _first(NEGATION, t)
    if refusal:
        return IntentResult(False, _topic(t), lang, 0.9, SOURCE, refusal)
    deletion = ""
    for sentence in _SENTENCE.split(t):
        if sentence and not _MARKETING.search(sentence):
            deletion = _first(DELETION, sentence)
            if deletion:
                break
    verify = _first(VERIFICATION, t)
    if verify and not IMPERATIVE.search(t):
        return IntentResult(True, "deletion_verification", lang, 0.85, SOURCE, verify)
    if deletion:
        return IntentResult(True, "deletion", lang, 0.9, SOURCE, deletion)
    if verify:
        return IntentResult(True, "deletion_verification", lang, 0.85, SOURCE, verify)
    topic = _topic(t)
    confidence = 0.3 if topic == "other" and not TOPICS[-1][1].search(t) else 0.8
    return IntentResult(False, topic, lang, confidence, SOURCE, "")
