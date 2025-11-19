# src/hockey_logic.py

from __future__ import annotations

from typing import List, Optional

from .khl_form_client import TeamForm


def _safe_int(value, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _estimate_points_from_form(form: Optional[TeamForm]) -> int:
    """
    Грубая оценка 'очков' по форме.
    Если в TeamForm есть wins/losses/ot_losses — используем их.
    Если нет — просто возвращаем 0 (логика не сломается).
    """
    if form is None:
        return 0

    wins = _safe_int(getattr(form, "wins", 0))
    ot_wins = _safe_int(getattr(form, "ot_wins", 0))
    ot_losses = _safe_int(getattr(form, "ot_losses", 0))

    # Супер-приблизительно: 2 очка за победу, 2 за победу в ОТ, 1 за поражение в ОТ
    points = wins * 2 + ot_wins * 2 + ot_losses * 1
    return points


def _describe_totals(form: Optional[TeamForm]) -> str | None:
    """
    Короткое описание тоталов по команде.
    Ожидаем, что в TeamForm могут быть:
    - avg_scored / avg_allowed / avg_total
    Если чего-то нет — просто молчим.
    """
    if form is None:
        return None

    avg_scored = getattr(form, "avg_scored", None)
    avg_allowed = getattr(form, "avg_allowed", None)
    avg_total = getattr(form, "avg_total", None)

    parts: List[str] = []
    if isinstance(avg_scored, (int, float)):
        parts.append(f"забивают в среднем {avg_scored:.1f}")
    if isinstance(avg_allowed, (int, float)):
        parts.append(f"пропускают {avg_allowed:.1f}")
    if isinstance(avg_total, (int, float)):
        parts.append(f"средний тотал ≈ {avg_total:.1f}")

    if not parts:
        return None

    return ", ".join(parts)


def build_match_context_notes(
    team1_name: str,
    team2_name: str,
    form1: Optional[TeamForm],
    form2: Optional[TeamForm],
) -> List[str]:
    """
    Мини-хоккейная логика по матчу:
    - кто выглядит 'сильнее' по форме (top vs underdog)
    - есть ли риск странного матча (топ vs явный аутсайдер)
    - что важно смотреть по игре (тоталы / темп и т.п.)

    На вход:
    - названия команд (как в ev.team1 / ev.team2)
    - объекты TeamForm (могут быть None, тогда логика мягко деградирует)
    """
    notes: List[str] = []

    # --- 1. Оценка формально 'сильной' и 'слабой' команды по форме ---

    points1 = _estimate_points_from_form(form1)
    points2 = _estimate_points_from_form(form2)

    diff = points1 - points2

    if diff > 4:
        notes.append(
            f"{team1_name} выглядит сильнее по недавней форме, чем {team2_name} "
            f"(по очкам за последние матчи есть ощутимый запас)."
        )
        strong_team = team1_name
        weak_team = team2_name
        strong_form = form1
        weak_form = form2
        strong_is_home = True  # условно, потом можно доработать
    elif diff < -4:
        notes.append(
            f"{team2_name} выглядит сильнее по недавней форме, чем {team1_name} "
            f"(по очкам за последние матчи есть ощутимый запас)."
        )
        strong_team = team2_name
        weak_team = team1_name
        strong_form = form2
        weak_form = form1
        strong_is_home = False
    else:
        strong_team = ""
        weak_team = ""
        strong_form = None
        weak_form = None
        strong_is_home = True
        notes.append(
            f"По недавней форме {team1_name} и {team2_name} выглядят довольно близко друг к другу."
        )

    # --- 2. 'Риск странного матча' (условный сценарий, о котором ты говорил) ---

    weird_risk = 0.0
    if strong_team:
        # Если сильная команда идёт заметно лучше, а слабая откровенно проседает —
        # появляется риск 'недонастроя' фаворита.
        weak_points = _estimate_points_from_form(weak_form)
        if weak_points <= 4 and abs(diff) >= 6:
            weird_risk = 0.7
            notes.append(
                f"Есть риск странного матча: {strong_team} явно сильнее по форме, "
                f"{weak_team} смотрится аутсайдером. В таких играх фаворит иногда "
                f"экономит силы и даёт сопернику слишком много шансов."
            )

    if 0.3 < weird_risk < 0.7:
        notes.append(
            "Риск 'странного' сценария (недонастрой фаворита, экономия сил, неожиданные провалы) — умеренный."
        )
    elif weird_risk >= 0.7:
        notes.append(
            "Риск 'странного' сценария (недонастрой фаворита, экономия сил под более важные матчи) — повышенный."
        )

    # --- 3. Контекст по тоталам ---

    desc1 = _describe_totals(form1)
    desc2 = _describe_totals(form2)

    if desc1:
        notes.append(f"{team1_name}: {desc1}.")
    if desc2:
        notes.append(f"{team2_name}: {desc2}.")

    # Если это ярко атакующая команда против более закрытой — подсветим это.
    if form1 and form2:
        avg1 = getattr(form1, "avg_total", None)
        avg2 = getattr(form2, "avg_total", None)
        if isinstance(avg1, (int, float)) and isinstance(avg2, (int, float)):
            if avg1 - avg2 >= 1.0:
                notes.append(
                    f"{team1_name} играют в более 'верхний' хоккей по тоталам, чем {team2_name}. "
                    "Это важно учитывать, если смотришь в сторону тоталов."
                )
            elif avg2 - avg1 >= 1.0:
                notes.append(
                    f"{team2_name} играют в более 'верхний' хоккей по тоталам, чем {team1_name}. "
                    "Это важно учитывать, если смотришь в сторону тоталов."
                )

    return notes
