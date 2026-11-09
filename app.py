
import os
import time
import html
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import streamlit as st
import folium
from streamlit_folium import st_folium
from streamlit_geolocation import streamlit_geolocation


# ============================================================
# 1. 기본 설정
# ============================================================

st.set_page_config(
    page_title="우리 동네 공중화장실 찾기",
    page_icon="🚻",
    layout="wide",
    initial_sidebar_state="collapsed",
)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

SOURCE_FILE = DATA_DIR / "공중화장실정보.csv"
PUBLIC_COORD_FILE = DATA_DIR / "공중화장실좌표.csv"
CACHE_FILE = DATA_DIR / "kakao_coordinate_cache.csv"

KAKAO_ADDRESS_URL = "https://dapi.kakao.com/v2/local/search/address.json"
KAKAO_KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"

DEFAULT_LAT = 37.4979
DEFAULT_LON = 127.0276


# ============================================================
# 2. 화면 디자인
# ============================================================

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Pretendard:wght@400;500;600;700;800&display=swap');

html, body, [class*="css"] {
    font-family: Pretendard, sans-serif;
}

.stApp {
    background: #f5f7fb;
}

.block-container {
    max-width: 1350px;
    padding-top: 1.5rem;
    padding-bottom: 3rem;
}

.hero {
    background: linear-gradient(120deg, #123b32, #16765c, #2c9b76);
    padding: 32px;
    border-radius: 22px;
    color: white;
    margin-bottom: 24px;
    box-shadow: 0 10px 28px rgba(18,59,50,.14);
}

.hero h1 {
    color: white;
    font-size: 2rem;
    font-weight: 800;
    margin: 0 0 8px 0;
}

.hero p {
    color: #e4f5ee;
    margin: 0;
}

.metric {
    background: white;
    padding: 18px;
    border-radius: 16px;
    border: 1px solid #e8edf2;
    min-height: 90px;
}

.metric-label {
    color: #718096;
    font-size: .85rem;
    margin-bottom: 7px;
}

.metric-value {
    color: #172b25;
    font-size: 1.4rem;
    font-weight: 800;
}

.section-title {
    font-size: 1.15rem;
    font-weight: 800;
    color: #1c3029;
    margin: 22px 0 12px;
}

div[data-testid="stTextInput"] input {
    border-radius: 12px;
}

div.stButton > button {
    border-radius: 11px;
    font-weight: 700;
    min-height: 2.7rem;
}

.hint {
    color: #718096;
    font-size: .85rem;
}
</style>
""", unsafe_allow_html=True)


# ============================================================
# 3. 공통 함수
# ============================================================

def get_secret(name, default=""):
    try:
        value = st.secrets.get(name, default)
        return str(value).strip() if value is not None else default
    except Exception:
        return os.getenv(name, default).strip()


def clean_text(value):
    if pd.isna(value):
        return ""
    return str(value).strip()


def load_csv(path):
    if not path.exists():
        return None

    for encoding in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
        try:
            return pd.read_csv(
                path,
                encoding=encoding,
                low_memory=False,
                dtype=str,
            )
        except Exception:
            continue

    return None


def find_col(df, candidates):
    for col in candidates:
        if col in df.columns:
            return col
    return None


def get_address_series(df):
    road_col = find_col(
        df,
        ["소재지도로명주소", "도로명주소", "도로명"],
    )

    lot_col = find_col(
        df,
        ["소재지지번주소", "지번주소", "소재지주소"],
    )

    if road_col:
        road = df[road_col].fillna("").astype(str).str.strip()
    else:
        road = pd.Series("", index=df.index)

    if lot_col:
        lot = df[lot_col].fillna("").astype(str).str.strip()
    else:
        lot = pd.Series("", index=df.index)

    return road.where(road.ne(""), lot)


def get_cache_address_column(df):
    return find_col(
        df,
        [
            "주소_좌표변환용",
            "주소",
            "address",
            "도로명주소",
            "소재지도로명주소",
            "소재지지번주소",
        ],
    )


def get_cache_map():
    cache = load_csv(CACHE_FILE)

    if cache is None or cache.empty:
        return {}

    address_col = get_cache_address_column(cache)
    lat_col = find_col(cache, ["위도", "lat", "latitude", "y"])
    lon_col = find_col(cache, ["경도", "lon", "lng", "longitude", "x"])

    if not all([address_col, lat_col, lon_col]):
        return {}

    cache["_주소"] = cache[address_col].fillna("").astype(str).str.strip()
    cache["_위도"] = pd.to_numeric(cache[lat_col], errors="coerce")
    cache["_경도"] = pd.to_numeric(cache[lon_col], errors="coerce")

    cache = cache.dropna(subset=["_위도", "_경도"])
    cache = cache[cache["_주소"] != ""]

    result = {}

    for _, row in cache.iterrows():
        address = row["_주소"]
        lat = float(row["_위도"])
        lon = float(row["_경도"])

        if -90 <= lat <= 90 and -180 <= lon <= 180:
            result[address] = (lat, lon)

    return result


def valid_coordinates(df):
    if df is None or df.empty:
        return pd.DataFrame()

    if not {"위도", "경도"}.issubset(df.columns):
        return pd.DataFrame()

    result = df.copy()

    result["위도"] = pd.to_numeric(result["위도"], errors="coerce")
    result["경도"] = pd.to_numeric(result["경도"], errors="coerce")

    result = result.dropna(subset=["위도", "경도"])

    result = result[
        result["위도"].between(-90, 90)
        & result["경도"].between(-180, 180)
    ].copy()

    return result


# ============================================================
# 4. 원본 + 캐시 자동 결합
# ============================================================

def build_public_data():
    source = load_csv(SOURCE_FILE)

    if source is None:
        return None, "원본 공중화장실정보.csv 파일을 찾을 수 없습니다."

    source = source.copy()

    addresses = get_address_series(source)
    source["주소_좌표변환용"] = addresses.fillna("").astype(str).str.strip()

    cache_map = get_cache_map()

    if "위도" in source.columns:
        source["위도"] = pd.to_numeric(source["위도"], errors="coerce")
    else:
        source["위도"] = np.nan

    if "경도" in source.columns:
        source["경도"] = pd.to_numeric(source["경도"], errors="coerce")
    else:
        source["경도"] = np.nan

    for idx, address in source["주소_좌표변환용"].items():
        if address in cache_map:
            lat, lon = cache_map[address]

            if pd.isna(source.at[idx, "위도"]):
                source.at[idx, "위도"] = lat

            if pd.isna(source.at[idx, "경도"]):
                source.at[idx, "경도"] = lon

    result = valid_coordinates(source)

    if result.empty:
        return result, "유효한 좌표가 없습니다. 캐시 파일의 주소와 좌표 열을 확인해 주세요."

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    result.to_csv(
        PUBLIC_COORD_FILE,
        index=False,
        encoding="utf-8-sig",
    )

    return result, None


def load_map_data():
    if SOURCE_FILE.exists() and CACHE_FILE.exists():
        return build_public_data()

    public = load_csv(PUBLIC_COORD_FILE)

    if public is not None:
        public = valid_coordinates(public)

        if not public.empty:
            return public, None

    if SOURCE_FILE.exists():
        return build_public_data()

    return None, "지도 데이터 파일을 찾을 수 없습니다."


# ============================================================
# 5. 카카오 API
# ============================================================

def kakao_headers(api_key):
    return {"Authorization": f"KakaoAK {api_key}"}


def kakao_address_to_coord(session, address, api_key):
    try:
        response = session.get(
            KAKAO_ADDRESS_URL,
            headers=kakao_headers(api_key),
            params={"query": address, "size": 1},
            timeout=12,
        )
    except requests.RequestException as exc:
        return None, None, f"네트워크 오류: {exc}"

    if response.status_code == 401:
        return None, None, "401 인증 오류: REST API 키 확인 필요"

    if response.status_code == 403:
        return None, None, "403 권한 오류: 카카오 로컬 API 설정 확인 필요"

    if response.status_code == 429:
        return None, None, "429 요청 한도 초과"

    if response.status_code != 200:
        return None, None, f"HTTP {response.status_code}"

    try:
        docs = response.json().get("documents", [])

        if not docs:
            return None, None, "주소 검색 결과 없음"

        lon = float(docs[0]["x"])
        lat = float(docs[0]["y"])

        return lat, lon, None

    except Exception:
        return None, None, "좌표 응답을 읽지 못했습니다."


def kakao_keyword_search(query, api_key):
    if not api_key:
        return None, None, "카카오 API 키가 없습니다."

    try:
        response = requests.get(
            KAKAO_KEYWORD_URL,
            headers=kakao_headers(api_key),
            params={"query": query, "size": 1},
            timeout=10,
        )

        if response.status_code != 200:
            return None, None, f"장소 검색 실패: HTTP {response.status_code}"

        docs = response.json().get("documents", [])

        if not docs:
            return None, None, "검색 장소를 찾지 못했습니다."

        return float(docs[0]["y"]), float(docs[0]["x"]), None

    except Exception as exc:
        return None, None, f"장소 검색 오류: {exc}"


# ============================================================
# 6. 거리 계산 및 운영 상태
# ============================================================

def get_distances(df, lat, lon):
    lat1 = np.radians(float(lat))
    lon1 = np.radians(float(lon))

    lat2 = np.radians(df["위도"].astype(float))
    lon2 = np.radians(df["경도"].astype(float))

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        np.sin(dlat / 2) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    )

    return 6371 * 2 * np.arcsin(np.sqrt(a))


def get_operating_color(row):
    status_cols = [
        "운영여부",
        "개방여부",
        "운영상태",
        "화장실개방여부",
        "개방상태",
    ]

    text = " ".join(
        clean_text(row.get(col, ""))
        for col in status_cols
    ).lower()

    if any(x in text for x in ["폐쇄", "미운영", "운영중단", "미개방", "사용불가"]):
        return "gray"

    if any(x in text for x in ["운영", "개방", "상시", "가능"]):
        return "green"

    return "orange"


def popup_html(row, address_col, time_col, owner_col):
    address = clean_text(row.get(address_col, "")) if address_col else ""
    open_time = clean_text(row.get(time_col, "")) if time_col else ""
    owner = clean_text(row.get(owner_col, "")) if owner_col else ""

    address = html.escape(address or "정보 없음")
    open_time = html.escape(open_time or "정보 없음")
    owner = html.escape(owner or "정보 없음")

    return f"""
    <div style="font-family:Arial,sans-serif;width:230px;line-height:1.6">
        <b>주소</b><br>{address}<hr>
        <b>개방시간상세</b><br>{open_time}<hr>
        <b>화장실소유</b><br>{owner}
    </div>
    """


# ============================================================
# 7. 헤더
# ============================================================

st.markdown("""
<div class="hero">
    <h1>🚻 우리 동네 공중화장실 찾기</h1>
    <p>전국 공중화장실의 위치와 개방 정보를 지도에서 간편하게 확인하세요.</p>
</div>
""", unsafe_allow_html=True)

api_key = get_secret("KAKAO_REST_API_KEY")
admin_password = get_secret("ADMIN_PASSWORD")


# ============================================================
# 8. 데이터 불러오기
# ============================================================

with st.spinner("전국 공중화장실 데이터를 불러오는 중입니다..."):
    public_df, load_error = load_map_data()

if public_df is None:
    public_df = pd.DataFrame()

public_df = valid_coordinates(public_df)

if public_df.empty:
    st.error(load_error or "지도에 표시할 좌표 데이터가 없습니다.")
    st.info("관리자 메뉴에서 캐시 파일을 확인해 주세요.")

address_col = find_col(
    public_df,
    ["소재지도로명주소", "도로명주소", "소재지지번주소", "지번주소"],
)

time_col = find_col(
    public_df,
    ["개방시간상세", "개방시간", "개방시간상세내용"],
)

owner_col = find_col(
    public_df,
    ["화장실소유구분명", "화장실소유", "소유구분명"],
)


# ============================================================
# 9. 통계
# ============================================================

total_count = len(public_df)

c1, c2, c3 = st.columns(3)

with c1:
    st.markdown(
        f'<div class="metric"><div class="metric-label">지도 등록 화장실</div><div class="metric-value">{total_count:,}곳</div></div>',
        unsafe_allow_html=True,
    )

with c2:
    st.markdown(
        '<div class="metric"><div class="metric-label">검색 방식</div><div class="metric-value">지역 · 장소</div></div>',
        unsafe_allow_html=True,
    )

with c3:
    st.markdown(
        '<div class="metric"><div class="metric-label">데이터 연결</div><div class="metric-value">공공데이터</div></div>',
        unsafe_allow_html=True,
    )


# ============================================================
# 10. 현재 위치
# ============================================================

user_lat = None
user_lon = None
has_user_location = False

try:
    location_data = streamlit_geolocation()

    if isinstance(location_data, dict):
        raw_lat = location_data.get("latitude")
        raw_lon = location_data.get("longitude")

        if raw_lat is not None and raw_lon is not None:
            user_lat = float(raw_lat)
            user_lon = float(raw_lon)

            has_user_location = (
                -90 <= user_lat <= 90
                and -180 <= user_lon <= 180
            )

except Exception:
    has_user_location = False

if has_user_location:
    location_label = "현재 위치"
    center_lat = user_lat
    center_lon = user_lon
else:
    location_label = "강남역 기본 위치"
    center_lat = DEFAULT_LAT
    center_lon = DEFAULT_LON


# ============================================================
# 11. 검색
# ============================================================

st.markdown(
    '<div class="section-title">어디의 화장실을 찾으세요?</div>',
    unsafe_allow_html=True,
)

search_col, button_col = st.columns([5, 1])

with search_col:
    search_query = st.text_input(
        "지역 또는 장소 검색",
        placeholder="예: 경기도, 성남시, 강남역",
        label_visibility="collapsed",
    )

with button_col:
    search_clicked = st.button(
        "검색",
        type="primary",
        use_container_width=True,
    )

filtered = public_df.copy()
search_message = ""

if not public_df.empty and search_query.strip():
    query = search_query.strip()

    searchable_cols = [
        col for col in [
            "소재지도로명주소",
            "소재지지번주소",
            "도로명주소",
            "지번주소",
        ]
        if col in public_df.columns
    ]

    matched = pd.DataFrame()

    if searchable_cols:
        mask = pd.Series(False, index=public_df.index)

        for col in searchable_cols:
            mask |= (
                public_df[col]
                .fillna("")
                .astype(str)
                .str.contains(query, case=False, regex=False)
            )

        matched = public_df[mask].copy()

    if not matched.empty:
        filtered = matched

        center_lat = float(filtered["위도"].median())
        center_lon = float(filtered["경도"].median())

        search_message = f"'{query}' 주소 검색 결과 {len(filtered):,}곳"

    else:
        place_lat, place_lon, error = kakao_keyword_search(
            query,
            api_key,
        )

        if place_lat is not None:
            distances = get_distances(
                public_df,
                place_lat,
                place_lon,
            )

            filtered = public_df[distances <= 5].copy()

            center_lat = place_lat
            center_lon = place_lon

            search_message = (
                f"'{query}' 주변 5km 검색 결과 "
                f"{len(filtered):,}곳"
            )

        else:
            filtered = public_df.iloc[0:0].copy()
            search_message = error or "검색 결과가 없습니다."

if search_query.strip():
    st.caption(search_message)
else:
    st.caption(f"기본 지도 위치: {location_label}")


# ============================================================
# 12. 지도
# ============================================================

st.markdown(
    '<div class="section-title">화장실 위치 지도</div>',
    unsafe_allow_html=True,
)

zoom = 12 if search_query.strip() else 7

map_obj = folium.Map(
    location=[center_lat, center_lon],
    zoom_start=zoom,
    control_scale=True,
    tiles="OpenStreetMap",
)

# ------------------------------------------------------------
# 현재 위치 파란색 마커
# ------------------------------------------------------------

if has_user_location:

    folium.Marker(
        location=[user_lat, user_lon],
        tooltip="내 현재 위치",
        popup="📍 내 현재 위치",
        icon=folium.Icon(
            color="blue",
            icon="user",
            prefix="fa",
        ),
    ).add_to(map_obj)

    folium.Circle(
        location=[user_lat, user_lon],
        radius=100,
        color="#2878E5",
        fill=True,
        fill_color="#2878E5",
        fill_opacity=0.15,
        weight=2,
    ).add_to(map_obj)


# ------------------------------------------------------------
# 공중화장실 마커
# ------------------------------------------------------------

if not filtered.empty:

    map_rows = filtered.head(2500)

    for _, row in map_rows.iterrows():

        lat = float(row["위도"])
        lon = float(row["경도"])

        color = get_operating_color(row)

        popup = folium.Popup(
            popup_html(
                row,
                address_col,
                time_col,
                owner_col,
            ),
            max_width=280,
        )

        folium.Marker(
            location=[lat, lon],
            tooltip=(
                clean_text(row.get(address_col, "공중화장실"))
                if address_col
                else "공중화장실"
            ),
            popup=popup,
            icon=folium.Icon(
                color=color,
                icon="info-sign",
            ),
        ).add_to(map_obj)

    if len(filtered) > 2500:
        st.info(
            f"검색 결과 {len(filtered):,}곳 중 지도에는 "
            "최대 2,500곳을 표시합니다."
        )

else:
    st.info("검색 결과가 없습니다.")

st_folium(
    map_obj,
    width=None,
    height=590,
    returned_objects=[],
    key="public_toilet_map",
)


# ============================================================
# 13. 검색 결과 목록
# ============================================================

st.markdown(
    '<div class="section-title">검색 결과</div>',
    unsafe_allow_html=True,
)

if not filtered.empty:

    display_df = pd.DataFrame()

    display_df["주소"] = (
        filtered[address_col].fillna("")
        if address_col
        else ""
    )

    display_df["개방시간상세"] = (
        filtered[time_col].fillna("")
        if time_col
        else ""
    )

    display_df["화장실소유"] = (
        filtered[owner_col].fillna("")
        if owner_col
        else ""
    )

    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        height=300,
    )

else:
    st.info("표시할 화장실이 없습니다.")


# ============================================================
# 14. 관리자 메뉴
# ============================================================

with st.expander(
    "🔐 관리자 메뉴 (좌표 관리)",
    expanded=False,
):

    st.caption(
        "관리자 전용 기능입니다. "
        "좌표 변환 결과를 다운로드하여 GitHub data 폴더에 보관하세요."
    )

    if not admin_password:

        st.error(
            "Streamlit Secrets에 ADMIN_PASSWORD를 등록해 주세요."
        )

    else:

        entered_password = st.text_input(
            "관리자 비밀번호",
            type="password",
            key="admin_pw",
        )

        is_admin = entered_password == admin_password

        if not is_admin:

            st.caption("관리자 비밀번호를 입력해 주세요.")

        else:

            st.success("관리자 인증 완료")

            source_df = load_csv(SOURCE_FILE)

            if source_df is None:

                st.error("원본 공중화장실정보.csv를 찾을 수 없습니다.")

            else:

                source_addresses = get_address_series(source_df)

                source_df = source_df.copy()

                source_df["주소_좌표변환용"] = (
                    source_addresses.fillna("").astype(str).str.strip()
                )

                unique_addresses = sorted(
                    set(
                        a for a in source_df["주소_좌표변환용"]
                        if a
                    )
                )

                cache_map = get_cache_map()

                done_count = sum(
                    1 for address in unique_addresses
                    if address in cache_map
                )

                st.write(
                    f"전체 주소: **{len(unique_addresses):,}개**"
                )

                st.write(
                    f"기존 좌표 보유 주소: **{done_count:,}개**"
                )

                st.write(
                    f"새로 변환할 주소: **{len(unique_addresses) - done_count:,}개**"
                )

                # 기존 캐시 재결합
                if st.button(
                    "기존 캐시로 최종 좌표 파일 다시 만들기",
                    use_container_width=True,
                    key="rebuild_coordinates",
                ):

                    with st.spinner("기존 좌표를 원본 데이터와 결합하고 있습니다..."):

                        result, error = build_public_data()

                    if error:
                        st.error(error)

                    else:
                        st.success(
                            f"최종 파일 생성 완료! 지도 등록 가능 데이터: {len(result):,}개"
                        )

                    st.rerun()

                st.divider()

                # 추가 좌표 변환
                if st.button(
                    "남은 주소 좌표 변환 시작",
                    type="primary",
                    use_container_width=True,
                    key="start_geocoding",
                ):

                    pending = [
                        a for a in unique_addresses
                        if a not in cache_map
                    ]

                    if not api_key:

                        st.error("KAKAO_REST_API_KEY가 등록되지 않았습니다.")

                    elif not pending:

                        st.success("모든 주소의 좌표가 이미 준비되어 있습니다.")

                    else:

                        progress = st.progress(0.0)
                        status = st.empty()

                        session = requests.Session()

                        errors = []
                        success_count = 0
                        stopped = False

                        for idx, address in enumerate(pending, start=1):

                            lat, lon, error = kakao_address_to_coord(
                                session,
                                address,
                                api_key,
                            )

                            if error:

                                errors.append((address, error))

                                if error.startswith(
                                    ("401", "403", "429", "HTTP", "네트워크 오류")
                                ):

                                    stopped = True

                                    status.error(
                                        f"{idx:,}/{len(pending):,} 처리 중단: {error}"
                                    )

                                    break

                            else:

                                cache_map[address] = (lat, lon)
                                success_count += 1

                            progress.progress(
                                idx / len(pending)
                            )

                            if idx % 10 == 0 or idx == len(pending):

                                status.info(
                                    f"진행 중: {idx:,}/{len(pending):,} "
                                    f"· 이번 실행 성공: {success_count:,}개"
                                )

                            if idx % 100 == 0:

                                partial = pd.DataFrame([
                                    {
                                        "주소_좌표변환용": a,
                                        "위도": v[0],
                                        "경도": v[1],
                                        "변환상태": "성공",
                                    }
                                    for a, v in cache_map.items()
                                ])

                                partial.to_csv(
                                    CACHE_FILE,
                                    index=False,
                                    encoding="utf-8-sig",
                                )

                            time.sleep(0.12)

                        # 마지막 캐시 저장
                        cache_out = pd.DataFrame([
                            {
                                "주소_좌표변환용": a,
                                "위도": v[0],
                                "경도": v[1],
                                "변환상태": "성공",
                            }
                            for a, v in cache_map.items()
                        ])

                        cache_out.to_csv(
                            CACHE_FILE,
                            index=False,
                            encoding="utf-8-sig",
                        )

                        result, build_error = build_public_data()

                        if build_error:
                            st.error(build_error)
                        else:
                            st.success(
                                f"최종 좌표 파일 생성 완료: {len(result):,}개"
                            )

                        if stopped:
                            st.warning(
                                "일부 주소만 변환되었습니다. "
                                "저장된 좌표는 다음 실행에서도 재사용됩니다."
                            )
                        else:
                            st.success("좌표 변환 작업이 완료되었습니다.")

                        st.write(
                            f"이번 실행 성공: {success_count:,}개"
                        )

                        st.write(
                            f"좌표 보유 캐시: {len(cache_map):,}개"
                        )

                        if errors:

                            with st.expander("변환 실패 주소 확인"):

                                st.dataframe(
                                    pd.DataFrame(
                                        errors[:30],
                                        columns=["주소", "오류"],
                                    ),
                                    use_container_width=True,
                                    hide_index=True,
                                )

                        st.rerun()

                st.divider()

                # 데이터 다운로드
                if PUBLIC_COORD_FILE.exists():

                    with open(PUBLIC_COORD_FILE, "rb") as f:

                        st.download_button(
                            "📥 전국 좌표 데이터 다운로드",
                            data=f.read(),
                            file_name="공중화장실좌표.csv",
                            mime="text/csv",
                            use_container_width=True,
                        )

                if CACHE_FILE.exists():

                    with open(CACHE_FILE, "rb") as f:

                        st.download_button(
                            "📥 좌표 변환 캐시 다운로드",
                            data=f.read(),
                            file_name="kakao_coordinate_cache.csv",
                            mime="text/csv",
                            use_container_width=True,
                        )

                st.info(
                    "다운로드한 두 CSV 파일은 GitHub의 data 폴더에 "
                    "업로드하고 저장해야 다른 사용자에게도 공유됩니다."
                )


# ============================================================
# 15. 하단 안내
# ============================================================

st.markdown("---")

st.caption(
    "공중화장실 위치와 운영 정보는 공공데이터를 바탕으로 제공되며, "
    "실제 현장 상황과 다를 수 있습니다."
)
