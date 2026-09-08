# -*- coding: utf-8 -*-
"""
neis_meal_report.py
====================
나이스(NEIS) 교육정보 개방 포털의 학교급식식단정보 API를 이용해
- 학교별 "칼로리" 추이
- 학교별 "단백질" 추이
- 학교별 "가장 많이 나온 반찬" 순위
세 가지를 plotly 그래프(HTML 대시보드)로 만들어주는 스크립트입니다.

사용 예시
---------
python neis_meal_report.py --schools "서울고등학교" "부산중학교" --start 20250901 --end 20250930

인증키(KEY)가 있으면 더 안정적으로 조회할 수 있습니다.
python neis_meal_report.py --schools "서울고등학교" --start 20250901 --end 20250930 --key YOUR_NEIS_KEY

옵션 없이 실행하면 --help로 사용법을 볼 수 있습니다.
"""

import argparse
import re
import sys
from collections import Counter
from datetime import datetime

import requests
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

SCHOOL_INFO_URL = "https://open.neis.go.kr/hub/schoolInfo"
MEAL_INFO_URL = "https://open.neis.go.kr/hub/mealServiceDietInfo"


# ---------------------------------------------------------------------------
# 1. 학교 검색
# ---------------------------------------------------------------------------
def search_school(school_name: str, key: str | None = None) -> dict:
    """학교 이름으로 교육청 코드(ATPT_OFCDC_SC_CODE)와 학교 코드(SD_SCHUL_CODE)를 찾는다."""
    params = {"Type": "json", "SCHUL_NM": school_name}
    if key:
        params["KEY"] = key

    resp = requests.get(SCHOOL_INFO_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if "schoolInfo" not in data:
        raise ValueError(f"'{school_name}' 학교를 찾을 수 없습니다. (schoolInfo 응답 없음)")

    rows = data["schoolInfo"][1]["row"]
    if not rows:
        raise ValueError(f"'{school_name}' 학교를 찾을 수 없습니다. (검색 결과 없음)")

    if len(rows) > 1:
        names = ", ".join(f"{r['SCHUL_NM']}({r['LCTN_SC_NM']})" for r in rows[:5])
        print(
            f"[안내] '{school_name}' 검색 결과가 {len(rows)}건입니다. "
            f"첫 번째 결과를 사용합니다. (후보: {names})",
            file=sys.stderr,
        )

    row = rows[0]
    return {
        "school_name": row["SCHUL_NM"],
        "atpt_code": row["ATPT_OFCDC_SC_CODE"],
        "school_code": row["SD_SCHUL_CODE"],
        "region": row.get("LCTN_SC_NM", ""),
    }


# ---------------------------------------------------------------------------
# 2. 급식 데이터 파싱 유틸
# ---------------------------------------------------------------------------
def clean_dish_names(ddish_nm: str) -> list[str]:
    """DDISH_NM 문자열을 <br/> 기준으로 나누고, 뒤에 붙은 알레르기 번호 괄호를 제거한다."""
    items = ddish_nm.split("<br/>")
    cleaned = []
    for item in items:
        # 예: "제육볶음(5.6.13)" -> "제육볶음"
        name = re.sub(r"\([\d.\s]+\)\s*$", "", item).strip()
        # 별표, 세트 표시 등 잡문자 정리
        name = name.replace("*", "").strip()
        if name:
            cleaned.append(name)
    return cleaned


def extract_calorie(cal_info: str) -> float | None:
    """CAL_INFO 예: '645.6 Kcal' -> 645.6"""
    if not cal_info:
        return None
    m = re.search(r"[\d.]+", cal_info)
    return float(m.group()) if m else None


def extract_protein(ntr_info: str) -> float | None:
    """NTR_INFO 예: '탄수화물(g) : 90.0<br/>단백질(g) : 20.5<br/>...' 에서 단백질 값 추출"""
    if not ntr_info:
        return None
    m = re.search(r"단백질\s*\([^)]*\)\s*:\s*([\d.]+)", ntr_info)
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# 3. 급식 데이터 조회
# ---------------------------------------------------------------------------
def fetch_meals(
    atpt_code: str,
    school_code: str,
    start_ymd: str,
    end_ymd: str,
    key: str | None = None,
    meal_code: str = "2",  # 2 = 중식
) -> list[dict]:
    params = {
        "Type": "json",
        "ATPT_OFCDC_SC_CODE": atpt_code,
        "SD_SCHUL_CODE": school_code,
        "MMEAL_SC_CODE": meal_code,
        "MLSV_FROM_YMD": start_ymd,
        "MLSV_TO_YMD": end_ymd,
        "pSize": 999,
    }
    if key:
        params["KEY"] = key

    resp = requests.get(MEAL_INFO_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if "mealServiceDietInfo" not in data:
        # RESULT 상자만 온 경우 (해당 기간에 급식 데이터가 없음. 오류 아님)
        return []

    rows = data["mealServiceDietInfo"][1]["row"]
    records = []
    for row in rows:
        dishes = clean_dish_names(row.get("DDISH_NM", ""))
        cal = extract_calorie(row.get("CAL_INFO", ""))
        protein = extract_protein(row.get("NTR_INFO", ""))
        records.append(
            {
                "date": row["MLSV_YMD"],
                "dishes": dishes,
                "calorie": cal,
                "protein": protein,
            }
        )
    return records


# ---------------------------------------------------------------------------
# 4. 학교별 데이터 수집 (여러 학교)
# ---------------------------------------------------------------------------
def build_school_dataset(school_names: list[str], start_ymd: str, end_ymd: str, key: str | None):
    all_daily = []          # date/school/calorie/protein 테이블용
    dish_counters = {}      # school -> Counter

    for name in school_names:
        info = search_school(name, key)
        label = info["school_name"]
        print(f"[조회 중] {label} ({info['region']}) - 코드 {info['atpt_code']}/{info['school_code']}")

        meals = fetch_meals(info["atpt_code"], info["school_code"], start_ymd, end_ymd, key)
        if not meals:
            print(f"  -> 해당 기간에 급식 데이터가 없습니다.")
            continue

        counter = Counter()
        for m in meals:
            all_daily.append(
                {
                    "school": label,
                    "date": datetime.strptime(m["date"], "%Y%m%d"),
                    "calorie": m["calorie"],
                    "protein": m["protein"],
                }
            )
            counter.update(m["dishes"])
        dish_counters[label] = counter

    df = pd.DataFrame(all_daily)
    return df, dish_counters


# ---------------------------------------------------------------------------
# 5. plotly 그래프 3종 생성
# ---------------------------------------------------------------------------
def make_calorie_figure(df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    for school, g in df.groupby("school"):
        g = g.sort_values("date")
        fig.add_trace(go.Scatter(x=g["date"], y=g["calorie"], mode="lines+markers", name=school))
    fig.update_layout(
        title="학교별 급식 칼로리 추이",
        xaxis_title="날짜",
        yaxis_title="칼로리 (Kcal)",
        template="plotly_white",
    )
    return fig


def make_protein_figure(df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    for school, g in df.groupby("school"):
        g = g.sort_values("date")
        fig.add_trace(go.Scatter(x=g["date"], y=g["protein"], mode="lines+markers", name=school))
    fig.update_layout(
        title="학교별 급식 단백질 추이",
        xaxis_title="날짜",
        yaxis_title="단백질 (g)",
        template="plotly_white",
    )
    return fig


def make_top_dishes_figure(dish_counters: dict, top_n: int = 10) -> go.Figure:
    schools = list(dish_counters.keys())
    fig = make_subplots(
        rows=1,
        cols=len(schools),
        subplot_titles=[f"{s} - 최다 등장 반찬" for s in schools],
    )

    for i, school in enumerate(schools, start=1):
        top_items = dish_counters[school].most_common(top_n)
        if not top_items:
            continue
        names, counts = zip(*top_items)
        fig.add_trace(
            go.Bar(x=list(counts), y=list(names), orientation="h", name=school, showlegend=False),
            row=1,
            col=i,
        )
        fig.update_yaxes(autorange="reversed", row=1, col=i)

    fig.update_layout(
        title=f"학교별 급식 최다 등장 반찬 TOP {top_n}",
        template="plotly_white",
        height=500,
    )
    return fig


# ---------------------------------------------------------------------------
# 6. 세 그래프를 하나의 HTML로 합치기
# ---------------------------------------------------------------------------
def save_dashboard(fig_cal: go.Figure, fig_protein: go.Figure, fig_dishes: go.Figure, out_path: str):
    parts = [
        "<html><head><meta charset='utf-8'><title>학교 급식 리포트</title></head><body>",
        "<h1 style='font-family:sans-serif;'>학교 급식 리포트</h1>",
        fig_cal.to_html(full_html=False, include_plotlyjs="cdn"),
        fig_protein.to_html(full_html=False, include_plotlyjs=False),
        fig_dishes.to_html(full_html=False, include_plotlyjs=False),
        "</body></html>",
    ]
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


# ---------------------------------------------------------------------------
# 7. 메인
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="NEIS 급식 데이터 plotly 리포트 생성기")
    parser.add_argument("--schools", nargs="+", required=True, help="학교 이름 (여러 개 가능, 공백으로 구분)")
    parser.add_argument("--start", required=True, help="조회 시작일 YYYYMMDD")
    parser.add_argument("--end", required=True, help="조회 종료일 YYYYMMDD")
    parser.add_argument("--key", default=None, help="NEIS 오픈API 인증키 (선택)")
    parser.add_argument("--out", default="meal_report.html", help="출력 HTML 파일명")
    parser.add_argument("--csv", default="meal_data.csv", help="원본 데이터 CSV 저장 파일명")
    parser.add_argument("--top-n", type=int, default=10, help="최다 반찬 TOP N (기본 10)")
    args = parser.parse_args()

    df, dish_counters = build_school_dataset(args.schools, args.start, args.end, args.key)

    if df.empty:
        print("조회된 급식 데이터가 없습니다. 학교 이름/기간을 확인해주세요.", file=sys.stderr)
        sys.exit(1)

    df.to_csv(args.csv, index=False, encoding="utf-8-sig")
    print(f"[저장] 원본 데이터 -> {args.csv}")

    fig_cal = make_calorie_figure(df)
    fig_protein = make_protein_figure(df)
    fig_dishes = make_top_dishes_figure(dish_counters, top_n=args.top_n)

    save_dashboard(fig_cal, fig_protein, fig_dishes, args.out)
    print(f"[저장] 대시보드 -> {args.out}")

    # 콘솔 요약: 학교별 1위 반찬
    print("\n=== 학교별 가장 많이 나온 반찬 ===")
    for school, counter in dish_counters.items():
        if counter:
            top1, cnt = counter.most_common(1)[0]
            print(f"- {school}: {top1} ({cnt}회)")


if __name__ == "__main__":
    main()
