"""
cli.py - Command-line interface for the Claude Code usage dashboard.

Commands:
  scan      - Scan JSONL files and update the database
  today     - Print today's usage summary
  stats     - Print all-time usage statistics
  dashboard - Scan + open browser + start dashboard server
"""

import os
import sys
import sqlite3
from pathlib import Path
from datetime import datetime, date, timedelta

from scanner import VERSION, current_lang

DB_PATH = Path(os.environ.get("CLAUDE_USAGE_DB", Path.home() / ".claude" / "usage.db"))

# ── Localization ───────────────────────────────────────────────────────────────
# Terminal output language, resolved live from CLAUDE_USAGE_LANG (see
# scanner.current_lang). English is the default and stays the source of truth for
# the compact metric field codes in each row (turns=/in=/out=/cost=), which are
# left untranslated so column alignment holds across languages. Descriptive
# headers, labels, and notes are localized. Chinese ("zh") is opt-in via the env
# var or the `--lang` flag.
TRANSLATIONS = {
    "en": {
        "db_not_found": "Database not found. Run: python cli.py scan",
        "today_title": "Today's Usage",
        "today_none": "No usage recorded today.",
        "row_total": "TOTAL",
        "sessions_today": "Sessions today:",
        "subagent_tokens": "Subagent tokens:",
        "cache_read": "Cache read:",
        "cache_creation": "Cache creation:",
        "n_turns": "({turns} turns)",
        "week_title": "Weekly Usage",
        "date_to": "{start} to {end}",
        "week_none": "No usage recorded in the last 7 days.",
        "by_day": "By Day:",
        "by_model": "By Model:",
        "sessions_week": "Sessions this week:",
        "stats_title": "Claude Code Usage - All-Time Statistics",
        "period": "Period:",
        "total_sessions": "Total sessions:",
        "total_turns": "Total turns:",
        "subagent_turns": "Subagent turns:",
        "input_tokens": "Input tokens:",
        "output_tokens": "Output tokens:",
        "note_raw_prompt": "(raw prompt tokens)",
        "note_generated": "(generated tokens)",
        "note_cache_read": "(90% cheaper than input)",
        "note_cache_creation": "(25% premium on input)",
        "note_included": "(included in totals)",
        "est_total_cost": "Est. total cost:",
        "top_projects": "Top Projects:",
        "daily_avg_title": "Daily Average (last 30 days):",
        "avg_input": "Input:",
        "avg_output": "Output:",
        "scan_bg": "Scanning in the background...",
        "scan_bg_done": "Background scan complete.",
        "usage_header": "Claude Code Usage Dashboard",
        "usage_usage": "Usage:",
        "usage_scan": "Scan JSONL files and update database",
        "usage_today": "Show today's usage summary",
        "usage_week": "Show last 7 days (per-day + by-model)",
        "usage_stats": "Show all-time statistics",
        "usage_dashboard": "Scan + start dashboard (opens a browser unless --no-browser)",
        "usage_version": "Print the version and exit",
        "usage_options": "Options:",
        "usage_lang": "Output language (or set CLAUDE_USAGE_LANG); defaults to English",
    },
    "zh": {
        "db_not_found": "未找到数据库。请运行：python cli.py scan",
        "today_title": "今日用量",
        "today_none": "今日暂无用量记录。",
        "row_total": "合计",
        "sessions_today": "今日会话数：",
        "subagent_tokens": "子代理 Token：",
        "cache_read": "缓存读取：",
        "cache_creation": "缓存写入：",
        "n_turns": "（{turns} 轮）",
        "week_title": "本周用量",
        "date_to": "{start} 至 {end}",
        "week_none": "过去 7 天暂无用量记录。",
        "by_day": "按天：",
        "by_model": "按模型：",
        "sessions_week": "本周会话数：",
        "stats_title": "Claude Code 用量 - 全部统计",
        "period": "统计区间：",
        "total_sessions": "总会话数：",
        "total_turns": "总轮次：",
        "subagent_turns": "子代理轮次：",
        "input_tokens": "输入 Token：",
        "output_tokens": "输出 Token：",
        "note_raw_prompt": "（原始提示 Token）",
        "note_generated": "（生成的 Token）",
        "note_cache_read": "（比输入便宜 90%）",
        "note_cache_creation": "（在输入价上加价 25%）",
        "note_included": "（已计入总计）",
        "est_total_cost": "预估总费用：",
        "top_projects": "热门项目：",
        "daily_avg_title": "每日平均（近 30 天）：",
        "avg_input": "输入：",
        "avg_output": "输出：",
        "scan_bg": "正在后台扫描……",
        "scan_bg_done": "后台扫描完成。",
        "usage_header": "Claude Code 用量看板",
        "usage_usage": "用法：",
        "usage_scan": "扫描 JSONL 文件并更新数据库",
        "usage_today": "显示今日用量摘要",
        "usage_week": "显示最近 7 天（按天 + 按模型）",
        "usage_stats": "显示全部统计",
        "usage_dashboard": "扫描并启动看板（除非 --no-browser，否则会打开浏览器）",
        "usage_version": "打印版本并退出",
        "usage_options": "选项：",
        "usage_lang": "输出语言（或设置 CLAUDE_USAGE_LANG）；默认英文",
    },
    "es": {
        "db_not_found": "Base de datos no encontrada. Ejecute: python cli.py scan",
        "today_title": "Uso de hoy",
        "today_none": "No se registró uso hoy.",
        "row_total": "TOTAL",
        "sessions_today": "Sesiones hoy:",
        "subagent_tokens": "Tokens de subagentes:",
        "cache_read": "Lectura de caché:",
        "cache_creation": "Creación de caché:",
        "n_turns": "({turns} turnos)",
        "week_title": "Uso semanal",
        "date_to": "{start} a {end}",
        "week_none": "No se registró uso en los últimos 7 días.",
        "by_day": "Por día:",
        "by_model": "Por modelo:",
        "sessions_week": "Sesiones esta semana:",
        "stats_title": "Uso de Claude Code - Estadísticas de todo el tiempo",
        "period": "Período:",
        "total_sessions": "Sesiones totales:",
        "total_turns": "Turnos totales:",
        "subagent_turns": "Turnos de subagentes:",
        "input_tokens": "Tokens de entrada:",
        "output_tokens": "Tokens de salida:",
        "note_raw_prompt": "(tokens de prompt sin procesar)",
        "note_generated": "(tokens generados)",
        "note_cache_read": "(90% más barato que la entrada)",
        "note_cache_creation": "(25% de recargo sobre la entrada)",
        "note_included": "(incluido en los totales)",
        "est_total_cost": "Costo total est.:",
        "top_projects": "Proyectos principales:",
        "daily_avg_title": "Promedio diario (últimos 30 días):",
        "avg_input": "Entrada:",
        "avg_output": "Salida:",
        "scan_bg": "Escaneando en segundo plano...",
        "scan_bg_done": "Escaneo en segundo plano completo.",
        "usage_header": "Panel de uso de Claude Code",
        "usage_usage": "Uso:",
        "usage_scan": "Escanear archivos JSONL y actualizar la base de datos",
        "usage_today": "Mostrar el resumen de uso de hoy",
        "usage_week": "Mostrar los últimos 7 días (por día + por modelo)",
        "usage_stats": "Mostrar estadísticas de todo el tiempo",
        "usage_dashboard": "Escanear + iniciar el panel (abre un navegador salvo --no-browser)",
        "usage_version": "Imprimir la versión y salir",
        "usage_options": "Opciones:",
        "usage_lang": "Idioma de salida (o defina CLAUDE_USAGE_LANG); por defecto inglés",
    },
    "fr": {
        "db_not_found": "Base de données introuvable. Exécutez : python cli.py scan",
        "today_title": "Utilisation du jour",
        "today_none": "Aucune utilisation enregistrée aujourd'hui.",
        "row_total": "TOTAL",
        "sessions_today": "Sessions aujourd'hui :",
        "subagent_tokens": "Tokens de sous-agents :",
        "cache_read": "Lecture cache :",
        "cache_creation": "Création cache :",
        "n_turns": "({turns} tours)",
        "week_title": "Utilisation hebdomadaire",
        "date_to": "{start} à {end}",
        "week_none": "Aucune utilisation enregistrée ces 7 derniers jours.",
        "by_day": "Par jour :",
        "by_model": "Par modèle :",
        "sessions_week": "Sessions cette semaine :",
        "stats_title": "Utilisation de Claude Code - Statistiques globales",
        "period": "Période :",
        "total_sessions": "Sessions totales :",
        "total_turns": "Tours totaux :",
        "subagent_turns": "Tours de sous-agents :",
        "input_tokens": "Tokens d'entrée :",
        "output_tokens": "Tokens de sortie :",
        "note_raw_prompt": "(tokens de prompt bruts)",
        "note_generated": "(tokens générés)",
        "note_cache_read": "(90% moins cher que l'entrée)",
        "note_cache_creation": "(25% de majoration sur l'entrée)",
        "note_included": "(inclus dans les totaux)",
        "est_total_cost": "Coût total est. :",
        "top_projects": "Principaux projets :",
        "daily_avg_title": "Moyenne quotidienne (30 derniers jours) :",
        "avg_input": "Entrée :",
        "avg_output": "Sortie :",
        "scan_bg": "Analyse en arrière-plan...",
        "scan_bg_done": "Analyse en arrière-plan terminée.",
        "usage_header": "Tableau de bord d'utilisation de Claude Code",
        "usage_usage": "Usage :",
        "usage_scan": "Analyser les fichiers JSONL et mettre à jour la base de données",
        "usage_today": "Afficher le résumé d'utilisation du jour",
        "usage_week": "Afficher les 7 derniers jours (par jour + par modèle)",
        "usage_stats": "Afficher les statistiques globales",
        "usage_dashboard": "Analyser + démarrer le tableau de bord (ouvre un navigateur sauf --no-browser)",
        "usage_version": "Afficher la version et quitter",
        "usage_options": "Options :",
        "usage_lang": "Langue de sortie (ou définir CLAUDE_USAGE_LANG) ; anglais par défaut",
    },
    "de": {
        "db_not_found": "Datenbank nicht gefunden. Führen Sie aus: python cli.py scan",
        "today_title": "Heutige Nutzung",
        "today_none": "Heute keine Nutzung erfasst.",
        "row_total": "GESAMT",
        "sessions_today": "Sitzungen heute:",
        "subagent_tokens": "Subagent-Tokens:",
        "cache_read": "Cache-Lesen:",
        "cache_creation": "Cache-Erstellung:",
        "n_turns": "({turns} Züge)",
        "week_title": "Wöchentliche Nutzung",
        "date_to": "{start} bis {end}",
        "week_none": "In den letzten 7 Tagen keine Nutzung erfasst.",
        "by_day": "Nach Tag:",
        "by_model": "Nach Modell:",
        "sessions_week": "Sitzungen diese Woche:",
        "stats_title": "Claude Code Nutzung - Gesamtstatistik",
        "period": "Zeitraum:",
        "total_sessions": "Sitzungen gesamt:",
        "total_turns": "Züge gesamt:",
        "subagent_turns": "Subagent-Züge:",
        "input_tokens": "Eingabe-Tokens:",
        "output_tokens": "Ausgabe-Tokens:",
        "note_raw_prompt": "(rohe Prompt-Tokens)",
        "note_generated": "(generierte Tokens)",
        "note_cache_read": "(90% günstiger als Eingabe)",
        "note_cache_creation": "(25% Aufschlag auf Eingabe)",
        "note_included": "(in Summen enthalten)",
        "est_total_cost": "Gesch. Gesamtkosten:",
        "top_projects": "Top-Projekte:",
        "daily_avg_title": "Tagesdurchschnitt (letzte 30 Tage):",
        "avg_input": "Eingabe:",
        "avg_output": "Ausgabe:",
        "scan_bg": "Scannen im Hintergrund...",
        "scan_bg_done": "Hintergrund-Scan abgeschlossen.",
        "usage_header": "Claude Code Nutzungs-Dashboard",
        "usage_usage": "Verwendung:",
        "usage_scan": "JSONL-Dateien scannen und Datenbank aktualisieren",
        "usage_today": "Heutige Nutzungsübersicht anzeigen",
        "usage_week": "Letzte 7 Tage anzeigen (nach Tag + nach Modell)",
        "usage_stats": "Gesamtstatistik anzeigen",
        "usage_dashboard": "Scannen + Dashboard starten (öffnet einen Browser, außer --no-browser)",
        "usage_version": "Version ausgeben und beenden",
        "usage_options": "Optionen:",
        "usage_lang": "Ausgabesprache (oder CLAUDE_USAGE_LANG setzen); Standard Englisch",
    },
    "ja": {
        "db_not_found": "データベースが見つかりません。実行してください: python cli.py scan",
        "today_title": "今日の使用状況",
        "today_none": "今日の使用記録はありません。",
        "row_total": "合計",
        "sessions_today": "今日のセッション:",
        "subagent_tokens": "サブエージェントトークン:",
        "cache_read": "キャッシュ読取:",
        "cache_creation": "キャッシュ作成:",
        "n_turns": "({turns} ターン)",
        "week_title": "週間使用状況",
        "date_to": "{start} 〜 {end}",
        "week_none": "過去7日間の使用記録はありません。",
        "by_day": "日別:",
        "by_model": "モデル別:",
        "sessions_week": "今週のセッション:",
        "stats_title": "Claude Code 使用状況 - 全期間統計",
        "period": "期間:",
        "total_sessions": "総セッション数:",
        "total_turns": "総ターン数:",
        "subagent_turns": "サブエージェントターン:",
        "input_tokens": "入力トークン:",
        "output_tokens": "出力トークン:",
        "note_raw_prompt": "(生のプロンプトトークン)",
        "note_generated": "(生成トークン)",
        "note_cache_read": "(入力より90%安い)",
        "note_cache_creation": "(入力に25%上乗せ)",
        "note_included": "(合計に含む)",
        "est_total_cost": "推定合計コスト:",
        "top_projects": "上位プロジェクト:",
        "daily_avg_title": "日次平均 (過去30日):",
        "avg_input": "入力:",
        "avg_output": "出力:",
        "scan_bg": "バックグラウンドでスキャン中...",
        "scan_bg_done": "バックグラウンドスキャン完了。",
        "usage_header": "Claude Code 使用状況ダッシュボード",
        "usage_usage": "使い方:",
        "usage_scan": "JSONL ファイルをスキャンしてデータベースを更新",
        "usage_today": "今日の使用状況の概要を表示",
        "usage_week": "過去7日間を表示 (日別 + モデル別)",
        "usage_stats": "全期間の統計を表示",
        "usage_dashboard": "スキャン + ダッシュボード起動 (--no-browser 以外はブラウザを開く)",
        "usage_version": "バージョンを表示して終了",
        "usage_options": "オプション:",
        "usage_lang": "出力言語 (または CLAUDE_USAGE_LANG を設定)。既定は英語",
    },
    "ko": {
        "db_not_found": "데이터베이스를 찾을 수 없습니다. 실행하세요: python cli.py scan",
        "today_title": "오늘 사용량",
        "today_none": "오늘 기록된 사용량이 없습니다.",
        "row_total": "합계",
        "sessions_today": "오늘 세션:",
        "subagent_tokens": "서브에이전트 토큰:",
        "cache_read": "캐시 읽기:",
        "cache_creation": "캐시 생성:",
        "n_turns": "({turns} 턴)",
        "week_title": "주간 사용량",
        "date_to": "{start} ~ {end}",
        "week_none": "지난 7일간 기록된 사용량이 없습니다.",
        "by_day": "일별:",
        "by_model": "모델별:",
        "sessions_week": "이번 주 세션:",
        "stats_title": "Claude Code 사용량 - 전체 통계",
        "period": "기간:",
        "total_sessions": "총 세션:",
        "total_turns": "총 턴:",
        "subagent_turns": "서브에이전트 턴:",
        "input_tokens": "입력 토큰:",
        "output_tokens": "출력 토큰:",
        "note_raw_prompt": "(원시 프롬프트 토큰)",
        "note_generated": "(생성된 토큰)",
        "note_cache_read": "(입력보다 90% 저렴)",
        "note_cache_creation": "(입력에 25% 추가)",
        "note_included": "(합계에 포함)",
        "est_total_cost": "예상 총 비용:",
        "top_projects": "상위 프로젝트:",
        "daily_avg_title": "일일 평균 (지난 30일):",
        "avg_input": "입력:",
        "avg_output": "출력:",
        "scan_bg": "백그라운드에서 스캔 중...",
        "scan_bg_done": "백그라운드 스캔 완료.",
        "usage_header": "Claude Code 사용량 대시보드",
        "usage_usage": "사용법:",
        "usage_scan": "JSONL 파일을 스캔하고 데이터베이스 업데이트",
        "usage_today": "오늘 사용량 요약 표시",
        "usage_week": "최근 7일 표시 (일별 + 모델별)",
        "usage_stats": "전체 통계 표시",
        "usage_dashboard": "스캔 + 대시보드 시작 (--no-browser가 아니면 브라우저 열기)",
        "usage_version": "버전을 출력하고 종료",
        "usage_options": "옵션:",
        "usage_lang": "출력 언어 (또는 CLAUDE_USAGE_LANG 설정), 기본값 영어",
    },
    "pt": {
        "db_not_found": "Banco de dados não encontrado. Execute: python cli.py scan",
        "today_title": "Uso de hoje",
        "today_none": "Nenhum uso registrado hoje.",
        "row_total": "TOTAL",
        "sessions_today": "Sessões hoje:",
        "subagent_tokens": "Tokens de subagentes:",
        "cache_read": "Leitura de cache:",
        "cache_creation": "Criação de cache:",
        "n_turns": "({turns} turnos)",
        "week_title": "Uso semanal",
        "date_to": "{start} a {end}",
        "week_none": "Nenhum uso registrado nos últimos 7 dias.",
        "by_day": "Por dia:",
        "by_model": "Por modelo:",
        "sessions_week": "Sessões esta semana:",
        "stats_title": "Uso do Claude Code - Estatísticas de todo o período",
        "period": "Período:",
        "total_sessions": "Sessões totais:",
        "total_turns": "Turnos totais:",
        "subagent_turns": "Turnos de subagentes:",
        "input_tokens": "Tokens de entrada:",
        "output_tokens": "Tokens de saída:",
        "note_raw_prompt": "(tokens de prompt brutos)",
        "note_generated": "(tokens gerados)",
        "note_cache_read": "(90% mais barato que a entrada)",
        "note_cache_creation": "(25% de acréscimo sobre a entrada)",
        "note_included": "(incluído nos totais)",
        "est_total_cost": "Custo total est.:",
        "top_projects": "Principais projetos:",
        "daily_avg_title": "Média diária (últimos 30 dias):",
        "avg_input": "Entrada:",
        "avg_output": "Saída:",
        "scan_bg": "Escaneando em segundo plano...",
        "scan_bg_done": "Escaneamento em segundo plano concluído.",
        "usage_header": "Painel de uso do Claude Code",
        "usage_usage": "Uso:",
        "usage_scan": "Escanear arquivos JSONL e atualizar o banco de dados",
        "usage_today": "Mostrar o resumo de uso de hoje",
        "usage_week": "Mostrar os últimos 7 dias (por dia + por modelo)",
        "usage_stats": "Mostrar estatísticas de todo o período",
        "usage_dashboard": "Escanear + iniciar o painel (abre um navegador exceto --no-browser)",
        "usage_version": "Imprimir a versão e sair",
        "usage_options": "Opções:",
        "usage_lang": "Idioma de saída (ou defina CLAUDE_USAGE_LANG); padrão inglês",
    },
    "ru": {
        "db_not_found": "База данных не найдена. Выполните: python cli.py scan",
        "today_title": "Использование сегодня",
        "today_none": "За сегодня использование не зафиксировано.",
        "row_total": "ИТОГО",
        "sessions_today": "Сессий сегодня:",
        "subagent_tokens": "Токены субагентов:",
        "cache_read": "Чтение кэша:",
        "cache_creation": "Создание кэша:",
        "n_turns": "({turns} обменов)",
        "week_title": "Использование за неделю",
        "date_to": "{start} — {end}",
        "week_none": "За последние 7 дней использование не зафиксировано.",
        "by_day": "По дням:",
        "by_model": "По модели:",
        "sessions_week": "Сессий за неделю:",
        "stats_title": "Использование Claude Code - Статистика за всё время",
        "period": "Период:",
        "total_sessions": "Всего сессий:",
        "total_turns": "Всего обменов:",
        "subagent_turns": "Обмены субагентов:",
        "input_tokens": "Токены ввода:",
        "output_tokens": "Токены вывода:",
        "note_raw_prompt": "(исходные токены промпта)",
        "note_generated": "(сгенерированные токены)",
        "note_cache_read": "(на 90% дешевле ввода)",
        "note_cache_creation": "(наценка 25% к вводу)",
        "note_included": "(включено в итоги)",
        "est_total_cost": "Оцен. общая стоимость:",
        "top_projects": "Топ проектов:",
        "daily_avg_title": "Средн. за день (последние 30 дней):",
        "avg_input": "Ввод:",
        "avg_output": "Вывод:",
        "scan_bg": "Сканирование в фоне...",
        "scan_bg_done": "Фоновое сканирование завершено.",
        "usage_header": "Панель использования Claude Code",
        "usage_usage": "Использование:",
        "usage_scan": "Сканировать файлы JSONL и обновить базу данных",
        "usage_today": "Показать сводку использования за сегодня",
        "usage_week": "Показать последние 7 дней (по дням + по модели)",
        "usage_stats": "Показать статистику за всё время",
        "usage_dashboard": "Сканировать + запустить панель (открывает браузер, кроме --no-browser)",
        "usage_version": "Вывести версию и выйти",
        "usage_options": "Параметры:",
        "usage_lang": "Язык вывода (или задайте CLAUDE_USAGE_LANG); по умолчанию английский",
    },
}


def t(key, **kw):
    lang = current_lang()
    s = TRANSLATIONS.get(lang, {}).get(key)
    if s is None:
        s = TRANSLATIONS["en"].get(key, key)
    return s.format(**kw) if kw else s


def lbl(key, width):
    """Translated label padded to `width`, but always with at least one trailing
    space. Pure str.ljust would leave no gap when a translation is longer than
    `width` (common in other languages), so the value would abut the label."""
    s = t(key)
    return s + " " * max(1, width - len(s))

PRICING = {
    # Fable / Mythos — Anthropic's most capable class, priced at 2x Opus.
    # (Mythos 5 shares Fable 5's pricing; Project-Glasswing access only.)
    "claude-fable-5":    {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50},
    "claude-mythos-5":   {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50},
    "claude-opus-4-8":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-7":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-6":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-5":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-sonnet-4-7": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-7":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-haiku-4-6":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-haiku-4-5":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25},
}

def get_pricing(model):
    if not model:
        return None
    if model in PRICING:
        return PRICING[model]
    for key in PRICING:
        if model.startswith(key):
            return PRICING[key]
    # Substring fallback: match model family by keyword
    m = model.lower()
    if "fable" in m or "mythos" in m:
        return PRICING["claude-fable-5"]
    if "opus" in m:
        return PRICING["claude-opus-4-8"]
    if "sonnet" in m:
        return PRICING["claude-sonnet-4-6"]
    if "haiku" in m:
        return PRICING["claude-haiku-4-5"]
    return None

def calc_cost(model, inp, out, cache_read, cache_creation):
    p = get_pricing(model)
    if not p:
        return 0.0
    return (
        inp            * p["input"]       / 1_000_000 +
        out            * p["output"]      / 1_000_000 +
        cache_read     * p["cache_read"]  / 1_000_000 +
        cache_creation * p["cache_write"] / 1_000_000
    )

def fmt(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

def fmt_cost(c):
    return f"${c:.4f}"

def hr(char="-", width=60):
    print(char * width)

def require_db():
    if not DB_PATH.exists():
        print(t("db_not_found"))
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Ensure the schema is current before querying. The read commands query the
    # `agents` table and the `is_subagent`/`agent_id` columns, so a pre-existing
    # DB from before those were added would raise "no such column" when a read
    # command runs before the next scan migrates it. init_db is idempotent
    # (CREATE ... IF NOT EXISTS + additive column checks), so this is a cheap
    # no-op once migrated. Mirrors get_dashboard_data in dashboard.py.
    from scanner import init_db
    init_db(conn)
    return conn


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_scan(projects_dir=None):
    from scanner import scan
    scan(projects_dir=Path(projects_dir) if projects_dir else None)


def cmd_today():
    conn = require_db()
    today = date.today().isoformat()

    rows = conn.execute("""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            COUNT(*)                   as turns
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
        GROUP BY model
        ORDER BY inp + out DESC
    """, (today,)).fetchall()

    sessions = conn.execute("""
        SELECT COUNT(DISTINCT session_id) as cnt
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
    """, (today,)).fetchone()

    subagent = conn.execute("""
        SELECT
            COUNT(*) as turns,
            SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens) as tokens
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
          AND COALESCE(is_subagent, 0) = 1
    """, (today,)).fetchone()

    print()
    hr()
    print(f"  {t('today_title')}  ({today})")
    hr()

    if not rows:
        print(f"  {t('today_none')}")
        print()
        return

    total_inp = total_out = total_cr = total_cc = total_turns = 0
    total_cost = 0.0

    for r in rows:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        total_cost += cost
        total_inp += r["inp"] or 0
        total_out += r["out"] or 0
        total_cr  += r["cr"]  or 0
        total_cc  += r["cc"]  or 0
        total_turns += r["turns"]
        print(f"  {r['model']:<30}  turns={r['turns']:<4}  in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print(f"  {t('row_total'):<30}  turns={total_turns:<4}  in={fmt(total_inp):<8}  out={fmt(total_out):<8}  cost={fmt_cost(total_cost)}")
    print()
    print(f"  {lbl('sessions_today', 18)}{sessions['cnt']}")
    print(f"  {lbl('subagent_tokens', 18)}{fmt(subagent['tokens'] or 0)}  {t('n_turns', turns=fmt(subagent['turns'] or 0))}")
    print(f"  {lbl('cache_read', 18)}{fmt(total_cr)}")
    print(f"  {lbl('cache_creation', 18)}{fmt(total_cc)}")
    hr()
    print()
    conn.close()


def cmd_week():
    conn = require_db()

    today_d = date.today()
    start_d = today_d - timedelta(days=6)
    start = start_d.isoformat()
    end = today_d.isoformat()

    by_day_model = conn.execute("""
        SELECT
            substr(timestamp, 1, 10)   as day,
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            COUNT(*)                   as turns
        FROM turns
        WHERE substr(timestamp, 1, 10) BETWEEN ? AND ?
        GROUP BY day, model
    """, (start, end)).fetchall()

    by_model = conn.execute("""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            COUNT(*)                   as turns
        FROM turns
        WHERE substr(timestamp, 1, 10) BETWEEN ? AND ?
        GROUP BY model
        ORDER BY inp + out DESC
    """, (start, end)).fetchall()

    sessions = conn.execute("""
        SELECT COUNT(DISTINCT session_id) as cnt
        FROM turns
        WHERE substr(timestamp, 1, 10) BETWEEN ? AND ?
    """, (start, end)).fetchone()

    print()
    hr()
    print(f"  {t('week_title')}  ({t('date_to', start=start, end=end)})")
    hr()

    if not by_model:
        print(f"  {t('week_none')}")
        print()
        conn.close()
        return

    # Aggregate per-day across models (with per-turn cost attribution)
    per_day = {}
    for r in by_day_model:
        d = r["day"]
        bucket = per_day.setdefault(d, {"turns": 0, "inp": 0, "out": 0, "cost": 0.0})
        bucket["turns"] += r["turns"]
        bucket["inp"]   += r["inp"] or 0
        bucket["out"]   += r["out"] or 0
        bucket["cost"]  += calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)

    print(f"  {t('by_day')}")
    for i in range(7):
        d = (start_d + timedelta(days=i)).isoformat()
        b = per_day.get(d, {"turns": 0, "inp": 0, "out": 0, "cost": 0.0})
        print(f"    {d}  turns={b['turns']:<4}  in={fmt(b['inp']):<8}  out={fmt(b['out']):<8}  cost={fmt_cost(b['cost'])}")

    hr()
    print(f"  {t('by_model')}")

    total_inp = total_out = total_cr = total_cc = total_turns = 0
    total_cost = 0.0
    for r in by_model:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        total_cost  += cost
        total_inp   += r["inp"] or 0
        total_out   += r["out"] or 0
        total_cr    += r["cr"]  or 0
        total_cc    += r["cc"]  or 0
        total_turns += r["turns"]
        print(f"    {r['model']:<30}  turns={r['turns']:<4}  in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print(f"    {t('row_total'):<30}  turns={total_turns:<4}  in={fmt(total_inp):<8}  out={fmt(total_out):<8}  cost={fmt_cost(total_cost)}")
    print()
    print(f"  {lbl('sessions_week', 21)}{sessions['cnt']}")
    print(f"  {lbl('cache_read', 21)}{fmt(total_cr)}")
    print(f"  {lbl('cache_creation', 21)}{fmt(total_cc)}")
    hr()
    print()
    conn.close()


def cmd_stats():
    conn = require_db()

    # Session-level info (count, date range)
    session_info = conn.execute("""
        SELECT
            COUNT(*)                  as sessions,
            MIN(first_timestamp)      as first,
            MAX(last_timestamp)       as last
        FROM sessions
    """).fetchone()

    # All-time totals from turns (more accurate — per-turn model attribution)
    totals = conn.execute("""
        SELECT
            SUM(input_tokens)             as inp,
            SUM(output_tokens)            as out,
            SUM(cache_read_tokens)        as cr,
            SUM(cache_creation_tokens)    as cc,
            COUNT(*)                      as turns
        FROM turns
    """).fetchone()

    # By model from turns (each turn has the actual model used)
    by_model = conn.execute("""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            COUNT(*)                   as turns,
            COUNT(DISTINCT session_id) as sessions
        FROM turns
        GROUP BY model
        ORDER BY inp + out DESC
    """).fetchall()

    # Top 5 projects from turns (join with sessions for project name)
    top_projects = conn.execute("""
        SELECT
            COALESCE(s.project_name, 'unknown') as project_name,
            SUM(t.input_tokens)  as inp,
            SUM(t.output_tokens) as out,
            COUNT(*)             as turns,
            COUNT(DISTINCT t.session_id) as sessions
        FROM turns t
        LEFT JOIN sessions s ON t.session_id = s.session_id
        GROUP BY s.project_name
        ORDER BY inp + out DESC
        LIMIT 5
    """).fetchall()

    # Subagent totals (subagent tokens are included in the all-time totals above)
    subagent = conn.execute("""
        SELECT
            COUNT(*) as turns,
            SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens) as tokens
        FROM turns
        WHERE COALESCE(is_subagent, 0) = 1
    """).fetchone()

    # Daily average (last 30 days)
    daily_avg = conn.execute("""
        SELECT
            AVG(daily_inp) as avg_inp,
            AVG(daily_out) as avg_out
        FROM (
            SELECT
                substr(timestamp, 1, 10) as day,
                SUM(input_tokens) as daily_inp,
                SUM(output_tokens) as daily_out
            FROM turns
            WHERE timestamp >= datetime('now', '-30 days')
            GROUP BY day
        )
    """).fetchone()

    # Build total cost across all models
    total_cost = sum(
        calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        for r in by_model
    )

    print()
    hr("=")
    print(f"  {t('stats_title')}")
    hr("=")

    first_date = (session_info["first"] or "")[:10]
    last_date = (session_info["last"] or "")[:10]
    print(f"  {lbl('period', 18)}{t('date_to', start=first_date, end=last_date)}")
    print(f"  {lbl('total_sessions', 18)}{session_info['sessions'] or 0:,}")
    print(f"  {lbl('total_turns', 18)}{fmt(totals['turns'] or 0)}")
    print(f"  {lbl('subagent_turns', 18)}{fmt(subagent['turns'] or 0)}")
    print()
    print(f"  {lbl('input_tokens', 18)}{fmt(totals['inp'] or 0):<12}  {t('note_raw_prompt')}")
    print(f"  {lbl('output_tokens', 18)}{fmt(totals['out'] or 0):<12}  {t('note_generated')}")
    print(f"  {lbl('cache_read', 18)}{fmt(totals['cr'] or 0):<12}  {t('note_cache_read')}")
    print(f"  {lbl('cache_creation', 18)}{fmt(totals['cc'] or 0):<12}  {t('note_cache_creation')}")
    print(f"  {lbl('subagent_tokens', 18)}{fmt(subagent['tokens'] or 0):<12}  {t('note_included')}")
    print()
    print(f"  {lbl('est_total_cost', 18)}${total_cost:.4f}")
    hr()

    print(f"  {t('by_model')}")
    for r in by_model:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        print(f"    {r['model']:<30}  sessions={r['sessions']:<4}  turns={fmt(r['turns'] or 0):<6}  "
              f"in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print(f"  {t('top_projects')}")
    for r in top_projects:
        print(f"    {(r['project_name'] or 'unknown'):<40}  sessions={r['sessions']:<3}  "
              f"turns={fmt(r['turns'] or 0):<6}  tokens={fmt((r['inp'] or 0)+(r['out'] or 0))}")

    if daily_avg["avg_inp"]:
        hr()
        print(f"  {t('daily_avg_title')}")
        print(f"    {lbl('avg_input', 8)}{fmt(int(daily_avg['avg_inp'] or 0))}")
        print(f"    {lbl('avg_output', 8)}{fmt(int(daily_avg['avg_out'] or 0))}")

    hr("=")
    print()
    conn.close()


def cmd_dashboard(projects_dir=None, host=None, port=None, no_browser=False, surface=None):
    import threading
    import time

    from dashboard import serve

    host = host or os.environ.get("HOST", "localhost")
    port = int(port or os.environ.get("PORT", "8080"))

    # Bind and serve the port *first*, then scan in the background. A cold scan
    # over a large ~/.claude/projects backlog can take well over a minute, and
    # the VS Code extension kills the process if it doesn't answer /api/data
    # within ~10s (see vscode-extension/src/server-manager.ts). Serving up front
    # means the port is live immediately; the dashboard shows whatever's already
    # in the DB and auto-refreshes as the background scan commits new data.
    #
    # Capture cmd_scan into a local so the background thread closes over the
    # current binding — keeps the test suite's mock.patch(cli.cmd_scan) effective
    # and prevents the thread from ever touching the real DB after a patch lifts.
    scan = cmd_scan

    def background_scan():
        print(t("scan_bg"))
        scan(projects_dir=projects_dir)
        print(t("scan_bg_done"))

    threading.Thread(target=background_scan, daemon=True).start()

    # Open a browser for users running this as a script (see README). The VS Code
    # extension passes --no-browser since it embeds the dashboard in a webview.
    if not no_browser:
        import webbrowser

        def open_browser():
            time.sleep(1.0)
            webbrowser.open(f"http://{host}:{port}")

        threading.Thread(target=open_browser, daemon=True).start()

    serve(host=host, port=port, surface=surface)


# ── Entry point ───────────────────────────────────────────────────────────────

def usage_text():
    """Assemble the localized help text from translated pieces.

    Command-syntax lines stay in English (they are literal commands); only the
    header, section labels, and per-command descriptions are translated.
    """
    return f"""
{t('usage_header')}

{t('usage_usage')}
  python cli.py scan [--projects-dir PATH]   {t('usage_scan')}
  python cli.py today                        {t('usage_today')}
  python cli.py week                         {t('usage_week')}
  python cli.py stats                        {t('usage_stats')}
  python cli.py dashboard [--projects-dir PATH] [--host HOST] [--port PORT] [--no-browser] [--surface SURFACE]
                                                 {t('usage_dashboard')}
  python cli.py --version                    {t('usage_version')}

{t('usage_options')}
  --lang {{en|zh|es|fr|de|ja|ko|pt|ru}}       {t('usage_lang')}
"""

COMMANDS = {
    "scan": cmd_scan,
    "today": cmd_today,
    "week": cmd_week,
    "stats": cmd_stats,
    "dashboard": cmd_dashboard,
}

def parse_named_arg(args, flag):
    """Extract a --flag VALUE pair from an argument list."""
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            return args[i + 1]
    return None

def main():
    """Console entry point (``claude-usage``) and ``python cli.py`` dispatch."""
    # An explicit --lang wins over the ambient CLAUDE_USAGE_LANG for this run;
    # write it into the env so scanner.current_lang() (shared resolver) sees it too.
    lang = parse_named_arg(sys.argv[1:], "--lang")
    if lang:
        os.environ["CLAUDE_USAGE_LANG"] = lang

    if len(sys.argv) >= 2 and sys.argv[1] in ("--version", "-V", "version"):
        print(VERSION)
        sys.exit(0)

    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(usage_text())
        sys.exit(0)

    command = sys.argv[1]
    rest = sys.argv[2:]
    projects_dir = parse_named_arg(rest, "--projects-dir")

    if command == "dashboard":
        cmd_dashboard(
            projects_dir=projects_dir,
            host=parse_named_arg(rest, "--host"),
            port=parse_named_arg(rest, "--port"),
            no_browser="--no-browser" in rest,
            surface=parse_named_arg(rest, "--surface"),
        )
    elif command == "scan" and projects_dir:
        cmd_scan(projects_dir=projects_dir)
    else:
        COMMANDS[command]()


if __name__ == "__main__":
    main()
