import os, json, re
from sqlalchemy import create_engine, text

def clean_content(text):
    t = (text or "").strip()
    if not t:
        return t
    if "---JSON---" in t:
        t = t.split("---JSON---", 1)[0].strip()
    t2 = re.sub(
        r"\n?```(?:json|JSON)?\s*\n?\{[\s\S]*?\"(?:actions|monologue|ford_ask|mood|succession_gap|memory_writes)\"[\s\S]*?\}\s*\n?```\s*$",
        "",
        t,
        flags=re.I,
    )
    t = t2.strip()
    t2 = re.sub(
        r"\n\s*\{[\s\S]*\"(?:actions|monologue|ford_ask|mood|succession_gap|memory_writes)\"[\s\S]*\}\s*$",
        "",
        t,
    )
    t = t2.strip()
    if t.startswith("{") and '"monologue"' in t[:300]:
        try:
            obj = json.loads(t)
            if isinstance(obj, dict) and ("monologue" in obj or "actions" in obj):
                mono = (obj.get("monologue") or "").strip()
                ask = (obj.get("ford_ask") or "").strip()
                if mono and ask and ask not in mono:
                    return f"{mono}\n\n**What I need from you:** {ask}"
                return mono or ask or "Understood."
        except Exception:
            pass
    return t

url = os.environ["DATABASE_URL"]
eng = create_engine(url)
with eng.begin() as c:
    rows = c.execute(text("""
        SELECT id, content FROM ea_sovereign_desk_messages
        WHERE role='sovereign'
          AND (content LIKE :a OR content LIKE :b OR content LIKE :c)
        ORDER BY created_at DESC
        LIMIT 40
    """), {"a": '%"monologue"%', "b": "%---JSON---%", "c": "%```json%"}).fetchall()
    print("candidates", len(rows))
    updated = 0
    for mid, content in rows:
        cleaned = clean_content(content)
        if cleaned != content and cleaned:
            if len(cleaned) < len(content) or (content.lstrip().startswith("{") and not cleaned.lstrip().startswith("{")):
                c.execute(text("UPDATE ea_sovereign_desk_messages SET content=:c WHERE id=:i"),
                          {"c": cleaned[:12000], "i": mid})
                updated += 1
                print("fixed", mid, len(content), "->", len(cleaned))
    c.execute(text("""
        UPDATE ea_sovereign_memory SET value=:v, source=:s, updated_at=NOW()
        WHERE key='succession_gap'
    """), {"v": "Full succession granted 2026-07-16: money/Stripe, brand, hard-delete, HAR capture. Residual: only true Ford-only (2FA hardware, personal bank).", "s": "desk_fix"})
    print("updated messages", updated)
    for r in c.execute(text("""
        SELECT id, left(content, 160), created_at FROM ea_sovereign_desk_messages
        WHERE role='sovereign' ORDER BY created_at DESC LIMIT 3
    """)).fetchall():
        print("---", r[0], r[2])
        print(r[1])
print("done")
