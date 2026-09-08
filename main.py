# -*- coding: utf-8 -*-
"""
streamlit_app.py
=================
NEIS 학교급식 리포트 - Streamlit 웹 앱 버전

Streamlit Cloud 배포 방법
------------------------
1. 이 파일과 requirements.txt를 GitHub 저장소에 올린다.
2. https://share.streamlit.io 에서 저장소를 연결하고
   Main file path를 "streamlit_app.py"로 지정한다.
3. 배포되면 화면에서 학교 이름 / 기간 / 인증키를 입력하고 조회한다.

로컬 실행
--------
streamlit run streamlit_app.py
"""

import re
from collections import Counter
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import streamlit as st

SCHOOL_INFO_URL = "https://open.neis.go.kr/hub/schoolInfo"
MEAL_INFO_URL = "https://open.neis.go.kr/hub/mealServiceDietInfo"


# ---------------------------------------------------------------------------
# NEIS API 호출 / 파싱 함수 (main.py와 동일한 로직)
# ---------------------------------------------------------------------------
def search_school(school_name: str, key: str | None = None) -> dict:
    params = {"Type": "json", "SCHUL_NM": school_name}
    if key:
        params["KEY"] = key

    resp = requests.get(SCHOOL_INFO_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if "schoolInfo" not in data:
        raise ValueError(f"'{school_name}' 학교를 찾을 수 없습니다.")

    rows = data["schoolInfo"][1]["row"]
    if not rows:
        raise ValueError(f"'{school_name}' 학교를 찾을 수 없습니다.")

    row = rows[0]
    info = {
        "school_name": row["SCHUL_NM"],
        "atpt_code": row["ATPT_OFCDC_SC_CODE"],
        "school_code": row["SD_SCHUL_CODE"],
        "region": row.get("LCTN_SC_NM", ""),
    }
    if len(rows) > 1:
        candidates = ", ".join(f"{r['SCHUL_NM']}({r['LCTN_SC_NM']})" for r in rows[:5])
        st.info(f"'{school_name}' 검색 결과 {len(rows)}건 중 첫 번째를 사용합니다. (후보: {candidates})")
    return info


def clean_dish_names(ddish_nm: str) -> list[str]:
    items = ddish_nm.split("<br/>")
    cleaned = []
    for item in items:
        name = re.sub(r"\([\d.\s]+\)\s*$", "", item).strip()
        name = name.replace("*", "").strip()
        if name:
            cleaned.append(name)
    return cleaned


def extract_calorie(cal_info: str) -> float | None:
    if not cal_info:
        return None
    m = re.search(r"[\d.]+", cal_info)
    return float(m.group()) if m else None


def extract_protein(ntr_info: str) -> float | None:
    if not ntr_info:
        return None
    m = re.search(r"단백질\s*\([^)]*\)\s*:\s*([\d.]+)", ntr_info)
    return float(m.group(1)) if m else None


def fetch_meals(atpt_code, school_code, start_ymd, end_ymd, key=None, meal_code="2"):
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
        return []

    rows = data["mealServiceDietInfo"][1]["row"]
    records = []
    for row in rows:
        dishes = clean_dish_names(row.get("DDISH_NM", ""))
        cal = extract_calorie(row.get("CAL_INFO", ""))
        protein = extract_protein(row.get("NTR_INFO", ""))
        records.append({"date": row["MLSV_YMD"], "dishes": dishes, "calorie": cal, "protein": protein})
    return records


@st.cache_data(show_spinner=False, ttl=3600)
def build_school_dataset(school_names: tuple, start_ymd: str, end_ymd: str, key: str | None):
    all_daily = []
    dish_counters = {}
    logs = []

    for name in school_names:
        info = search_school(name, key)
        label = info["school_name"]
        logs.append(f"조회: {label} ({info['region']})")

        meals = fetch_meals(info["atpt_code"], info["school_code"], start_ymd, end_ymd, key)
        if not meals:
            logs.append(f"  -> '{label}' 해당 기간 급식 데이터 없음")
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
    return df, dish_counters, logs


# ---------------------------------------------------------------------------
# 그래프 생성 함수
# ---------------------------------------------------------------------------
def make_calorie_figure(df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    for school, g in df.groupby("school"):
        g = g.sort_values("date")
        fig.add_trace(go.Scatter(x=g["date"], y=g["calorie"], mode="lines+markers", name=school))
    fig.update_layout(title="학교별 급식 칼로리 추이", xaxis_title="날짜", yaxis_title="칼로리 (Kcal)", template="plotly_white")
    return fig


def make_protein_figure(df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    for school, g in df.groupby("school"):
        g = g.sort_values("date")
        fig.add_trace(go.Scatter(x=g["date"], y=g["protein"], mode="lines+markers", name=school))
    fig.update_layout(title="학교별 급식 단백질 추이", xaxis_title="날짜", yaxis_title="단백질 (g)", template="plotly_white")
    return fig


def make_top_dishes_figure(dish_counters: dict, top_n: int = 10) -> go.Figure:
    schools = list(dish_counters.keys())
    fig = make_subplots(rows=1, cols=max(len(schools), 1), subplot_titles=[f"{s} - 최다 반찬" for s in schools])

    for i, school in enumerate(schools, start=1):
        top_items = dish_counters[school].most_common(top_n)
        if not top_items:
            continue
        names, counts = zip(*top_items)
        fig.add_trace(
            go.Bar(x=list(counts), y=list(names), orientation="h", name=school, showlegend=False), row=1, col=i
        )
        fig.update_yaxes(autorange="reversed", row=1, col=i)

    fig.update_layout(title=f"학교별 급식 최다 등장 반찬 TOP {top_n}", template="plotly_white", height=500)
    return fig


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------
st.set_page_config(page_title="NEIS 학교급식 리포트", layout="wide")
st.title("🍱 학교 급식 리포트")
st.caption("나이스(NEIS) 오픈 API 기반 · 칼로리 / 단백질 추이 · 최다 등장 반찬")

with st.sidebar:
    st.header("조회 조건")
    schools_input = st.text_area(
        "학교 이름 (줄바꿈으로 구분, 여러 개 가능)",
        placeholder="예)\n서울고등학교\n경기중학교",
        height=100,
    )
    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("시작일", value=date.today() - timedelta(days=30))
    with col2:
        end_date = st.date_input("종료일", value=date.today())

    secret_key = st.secrets.get("KEY", "").strip() if hasattr(st, "secrets") else ""
    if secret_key:
        key = secret_key
        st.caption("✅ Secrets에 등록된 인증키를 사용합니다.")
    else:
        key = st.text_input("NEIS 인증키 (선택)", type="password")

    top_n = st.slider("최다 반찬 TOP N", min_value=3, max_value=20, value=10)
    run = st.button("조회하기", type="primary", use_container_width=True)

if run:
    school_names = tuple(s.strip() for s in schools_input.splitlines() if s.strip())
    if not school_names:
        st.warning("학교 이름을 한 개 이상 입력해주세요.")
        st.stop()

    start_ymd = start_date.strftime("%Y%m%d")
    end_ymd = end_date.strftime("%Y%m%d")

    with st.spinner("NEIS API에서 데이터를 가져오는 중..."):
        try:
            df, dish_counters, logs = build_school_dataset(school_names, start_ymd, end_ymd, key or None)
        except Exception as e:
            st.error(f"오류가 발생했습니다: {e}")
            st.stop()

    for log in logs:
        st.write(log)

    if df.empty:
        st.warning("조회된 급식 데이터가 없습니다. 학교 이름/기간을 확인해주세요.")
        st.stop()

    st.success(f"총 {len(df)}건의 급식 데이터를 불러왔습니다.")

    tab1, tab2, tab3, tab4 = st.tabs(["칼로리 추이", "단백질 추이", "최다 반찬", "원본 데이터"])

    with tab1:
        st.plotly_chart(make_calorie_figure(df), use_container_width=True)

    with tab2:
        st.plotly_chart(make_protein_figure(df), use_container_width=True)

    with tab3:
        st.plotly_chart(make_top_dishes_figure(dish_counters, top_n=top_n), use_container_width=True)
        st.subheader("학교별 1위 반찬")
        for school, counter in dish_counters.items():
            if counter:
                top1, cnt = counter.most_common(1)[0]
                st.write(f"- **{school}**: {top1} ({cnt}회)")

    with tab4:
        st.dataframe(df, use_container_width=True)
        csv = df.to_csv(index=False).encode("utf-8-sig")
        st.download_button("CSV 다운로드", data=csv, file_name="meal_data.csv", mime="text/csv")
else:
    st.info("왼쪽 사이드바에 학교 이름과 기간을 입력하고 '조회하기'를 눌러주세요.")
