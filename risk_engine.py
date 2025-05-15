#!/usr/bin/env python3
import argparse
import logging
import os
import sys

import pandas as pd
import yaml
from thefuzz import process
from jinja2 import Environment, FileSystemLoader

LOG_FORMAT = "[%(levelname)s] %(message)s"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Risk-Score Engine: автоматический расчёт клинических шкал"
    )
    parser.add_argument(
        "--input", "-i", required=True, help="Путь к CSV или Excel файлу с данными"
    )
    parser.add_argument(
        "--scores", "-s", required=True, help="YAML-файл с описанием шкал"
    )
    parser.add_argument(
        "--synonyms", "-y", required=True, help="YAML-файл со словарём синонимов"
    )
    parser.add_argument(
        "--out", "-o", required=True, help="Папка для отчётов (будет создана, если нет)"
    )
    return parser.parse_args()


def load_data(path: str) -> pd.DataFrame:
    if path.lower().endswith(".csv"):
        df = pd.read_csv(path)
    else:
        df = pd.read_excel(path)
    return df


def detect_orientation(df: pd.DataFrame, synonym_keys: list[str]) -> pd.DataFrame:
    matches = 0
    for col in df.columns:
        best, score = process.extractOne(col.lower().strip(), synonym_keys)
        if score >= 80:
            matches += 1
    if matches < 3:
        logging.info("Меняем ориентацию таблицы (транспонирование)")
        return df.T
    return df


def find_column(target_key: str, df: pd.DataFrame, score_cutoff=80) -> str | None:
    match, score = process.extractOne(target_key, df.columns, score_cutoff=score_cutoff)
    return match if match else None


def build_alias_map(df: pd.DataFrame, synonyms: dict) -> dict[str, str]:
    alias_map: dict[str, str] = {}
    for var, name_list in synonyms.items():
        norm_var = var.lower().strip()
        for cand in name_list:
            found = find_column(cand.lower().strip(), df)
            if found:
                alias_map[var] = found
                break
    return alias_map


def compute_scores_for_patient(
    row: pd.Series, alias_map: dict, scores_def: dict
) -> tuple[dict, dict]:
    computed: dict[str, float] = {}
    missed: dict[str, list[str]] = {}

    for score_name, cfg in scores_def.items():
        vars_cfg = cfg.get("variables", {})
        local_ns: dict = {}
        missing = []
        for var in vars_cfg:
            col = alias_map.get(var)
            if col and pd.notna(row.get(col)):
                local_ns[var] = row[col]
            else:
                missing.append(var)

        if missing:
            missed[score_name] = missing
            logging.warning(f"Пациент {row.name}: {score_name} пропущен — нет {missing}")
            continue

        try:
            exec(cfg["formula"], {}, local_ns)
            computed[score_name] = local_ns.get("score")
        except Exception as e:
            missed[score_name] = ["formula_error"]
            logging.warning(f"Пациент {row.name}: {score_name} пропущен — ошибка формулы ({e})")

    return computed, missed


def export_patient_report(
    pid: str,
    row: pd.Series,
    computed: dict,
    missed: dict,
    out_dir: str,
    template_env: Environment,
):
    # Excel
    out_xlsx = os.path.join(out_dir, f"patient_{pid}.xlsx")
    with pd.ExcelWriter(out_xlsx, engine="xlsxwriter") as writer:
        pd.DataFrame(row).to_excel(writer, sheet_name="Raw")
        df_scores = (
            pd.DataFrame.from_dict(computed, orient="index", columns=["value"])
            .rename_axis("score")
            .reset_index()
        )
        df_scores.to_excel(writer, sheet_name="Scores", index=False)
        workbook = writer.book
        worksheet = writer.sheets["Scores"]
        fmt = workbook.add_format({"font_color": "#0000FF"})
        worksheet.conditional_format(
            "B2:B100", {"type": "cell", "criteria": ">", "value": 0, "format": fmt}
        )

    # HTML
    template = template_env.get_template("report.html.j2")
    html = template.render(patient_id=pid, raw=row.to_dict(), computed=computed, missed=missed)
    out_html = os.path.join(out_dir, f"patient_{pid}.html")
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)
    args = parse_args()

    df = load_data(args.input)
    scores_def = yaml.safe_load(open(args.scores, encoding="utf-8"))
    synonyms = yaml.safe_load(open(args.synonyms, encoding="utf-8"))

    df = detect_orientation(df, list(synonyms.keys()))
    alias_map = build_alias_map(df, synonyms)

    os.makedirs(args.out, exist_ok=True)
    template_env = Environment(
        loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates")),
        autoescape=True,
    )

    summary = []
    logging.info(f"Loaded {len(df)} patients.")
    for pid, row in df.iterrows():
        computed, missed = compute_scores_for_patient(row, alias_map, scores_def)
        export_patient_report(pid, row, computed, missed, args.out, template_env)
        summary.append({
            "patient_id": pid,
            "computed_scores": ";".join(computed.keys()),
            "missed_scores": ";".join(missed.keys()),
        })

    summary_df = pd.DataFrame(summary)
    summary_df.to_csv(os.path.join(args.out, "summary.csv"), index=False)
    logging.info("Генерация отчётов завершена.")


if __name__ == "__main__":
    main()