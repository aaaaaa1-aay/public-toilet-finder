import html
import math
import os
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import folium
import pandas as pd
import requests
import streamlit as st
from folium.plugins import MarkerCluster
from streamlit_folium import st_folium
from streamlit_geolocation import streamlit_geolocation


# =========================================================
# 1. 페이지 설정 및 디자인
# =========================================================

st.set_page_config(
    page_title="우리 동네 공중화장실 찾기",
    page_icon="🚻",
    layout="wide",
    initial_sidebar_state="collapsed",
)

BASE_DIR = Path(__file__).resolve().parent
DATA_PATH = BASE_DIR / "data" / "공중화장실정보.csv"
CACHE_PATH = BASE_DIR / "data" / "kakao_coordinate_cache.csv"

KAKAO_ADDRESS_URL = "https://dapi.kakao.com/v2/local/search/address.json"
KAKAO_KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"

GANGNAM_LAT = 37.498095
GANGNAM_LON = 127.027610
SEARCH_RADIUS_KM = 10
SEOUL_TZ = ZoneInfo("Asia/Seoul")

st.markdown(
    """
    <style>
    .stApp {
        background: #f5f7fb;
    }
    .block-container {
        max-width: 1440px;
        padding-top: 1.5rem;
        padding-bottom: 3rem;
    }
    .hero {
        background: linear-gradient(120deg, #123b35 0%, #1e6b5b 60%, #3c927c 100%);
        padding: 30px 32px;
        border-radius: 22px;
        color: white;
        margin-bottom: 20px;
    }
    .hero h1 {
        color: white;
        font-size: 2rem;
        margin: 0 0 8px 0;
        font-weight: 800;
    }
    .hero p {
        color: #e2f3ee;
        margin: 0;
        font-size: 1rem;
    }
    .section-title {
        font-size: 1.15rem;
        font-weight: 750;
        color: #183b35;
        margin: 12px 0 8px 0;
    }
    .hint {
        color: #64748b;
        font-size: 0.9rem;
    }
    div[data-testid="stMetric"] {
        background: white;
        border: 1px solid #e7edf2;
        border-radius: 14px;
        padding: 12px 16px;
    }
    div[data-testid="stForm"] {
        background: white;
        border: 1px solid #e7edf2;
        border-radius: 16px;
        padding: 14px;
    }
    div.stButton > button, div.stFormSubmitButton > button {
        border-radius: 10px;
        font-weight: 700;
        min-height: 44px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="hero">
        <h1>🚻 우리 동네 공중화장실 찾기</h1>
        <p>현재 위치 또는 원하는 지역을 검색하고, 가까운 공중화장실을 지도에서 확인하세요.</p>
    </div>
    """,
    unsafe_allow_html=True,
)


# =========================================================
# 2. 설정값 및 공통 함수
# =========================================================

def get_secret(name, default=""):
    try:
        return str(st.secrets.get(name, default)).strip()
    except Exception:
        return default


KAKAO_API_KEY = get_secret("KAKAO_REST_API_KEY")
ADMIN_PASSWORD = get_secret("ADMIN_PASSWORD")


def clean_text(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def get_address(row):
    road = clean_text(row.get("소재지도로명주소", ""))
    lot = clean_text(row.get("소재지지번주소", ""))
    return road or lot


def safe_html(value):
    return html.escape(clean_text(value))


def load_csv():
    if not DATA_PATH.exists():
        raise FileNotFoundError(
            "data/공중화장실정보.csv 파일을 찾을 수 없습니다."
        )

    last_error = None
    for encoding in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            return pd.read_csv(
                DATA_PATH,
                encoding=encoding,
                dtype=str,
                low_memory=False,
            ).fillna("")
        except UnicodeDecodeError as error:
            last_error = error

    raise ValueError(f"CSV 인코딩을 읽을 수 없습니다: {last_error}")


@st.cache_data(show_spinner=False)
def cached_load_csv(file_path, modified_time):
    # 파일 수정 시 캐시를 새로 읽도록 modified_time을 인자로 받음
    return load_csv()


def load_coordinate_cache():
    if not CACHE_PATH.exists():
        return {}

    try:
        df = pd.read_csv(CACHE_PATH, dtype=str, encoding="utf-8-sig")
        if not {"주소", "위도", "경도"}.issubset(df.columns):
            return {}

        result = {}
        for _, row in df.iterrows():
            address = clean_text(row.get("주소"))
            try:
                lat = float(row.get("위도"))
                lon = float(row.get("경도"))
                if address and -90 <= lat <= 90 and -180 <= lon <= 180:
                    result[address] = (lat, lon)
            except (TypeError, ValueError):
                continue
        return result
    except Exception:
        return {}


def save_coordinate_cache(cache):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"주소": address, "위도": coords[0], "경도": coords[1]}
        for address, coords in cache.items()
    ]
    pd.DataFrame(rows, columns=["주소", "위도", "경도"]).to_csv(
        CACHE_PATH,
        index=False,
        encoding="utf-8-sig",
    )


# =========================================================
# 3. 카카오 API - 주소/장소 검색
# =========================================================

def kakao_request(url, params):
    if not KAKAO_API_KEY:
        return None, "NO_KEY"

    headers = {"Authorization": f"KakaoAK {KAKAO_API_KEY}"}

    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=12,
        )

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if response.status_code == 200:
            return payload, None

        api_code = payload.get("code")
        api_message = payload.get("msg") or payload.get("message") or ""

        if response.status_code == 429 or api_code == -10:
            return None, "LIMIT"

        if response.status_code == 401 or api_code == -401:
            return None, "AUTH"

        if response.status_code == 403:
            return None, f"PERMISSION:{api_message or 'API 사용 권한을 확인하세요.'}"

        if response.status_code >= 500:
            return None, "SERVER"

        return None, f"HTTP_{response.status_code}:{api_message}"

    except requests.Timeout:
        return None, "TIMEOUT"
    except requests.RequestException:
        return None, "NETWORK"


def geocode_address(address):
    payload, error = kakao_request(
        KAKAO_ADDRESS_URL,
        {"query": address},
    )
    if error:
        return None, error

    docs = payload.get("documents", [])
    if not docs:
        return None, "NOT_FOUND"

    try:
        doc = docs[0]
        return (float(doc["y"]), float(doc["x"])), None
    except (KeyError, TypeError, ValueError):
        return None, "BAD_RESPONSE"


def search_place(keyword):
    payload, error = kakao_request(
        KAKAO_KEYWORD_URL,
        {"query": keyword, "size": 1},
    )
    if error:
        return None, error

    docs = payload.get("documents", [])
    if not docs:
        return None, "NOT_FOUND"

    try:
        return (float(docs[0]["y"]), float(docs[0]["x"])), None
    except (KeyError, TypeError, ValueError):
        return None, "BAD_RESPONSE"


def error_message(error):
    messages = {
        "NO_KEY": "카카오 REST API 키가 Streamlit Secrets에 등록되지 않았습니다.",
        "LIMIT": "카카오 API 요청 한도에 도달했습니다. 잠시 후 다시 시도해 주세요.",
        "AUTH": "카카오 REST API 키 인증에 실패했습니다. 키 종류와 값을 확인해 주세요.",
        "SERVER": "카카오 서버에 일시적인 문제가 있습니다. 잠시 후 다시 시도해 주세요.",
        "TIMEOUT": "카카오 API 응답 시간이 초과되었습니다.",
        "NETWORK": "네트워크 연결을 확인해 주세요.",
        "NOT_FOUND": "검색 결과가 없습니다.",
        "BAD_RESPONSE": "카카오 API 응답에서 좌표를 확인하지 못했습니다.",
    }
    if not error:
        return ""
    if error.startswith("PERMISSION:"):
        return error.split(":", 1)[1]
    if error.startswith("HTTP_"):
        return f"카카오 API 요청 오류: {error}"
    return messages.get(error, f"알 수 없는 API 오류: {error}")


# =========================================================
# 4. 주소 좌표 변환 - 관리자 전용
# =========================================================

def convert_addresses(addresses, cache):
    unique_addresses = sorted({
        clean_text(address)
        for address in addresses
        if clean_text(address) and clean_text(address) not in cache
    })

    if not unique_addresses:
        st.success("변환할 새 주소가 없습니다. 좌표가 모두 준비되어 있습니다.")
        return cache

    if not KAKAO_API_KEY:
        st.error("카카오 REST API 키가 Secrets에 등록되지 않았습니다.")
        return cache

    st.info(f"새로 변환할 주소: {len(unique_addresses):,}개")

    progress = st.progress(0)
    status = st.empty()
    successes = 0
    not_found = 0
    failures = 0
    first_error = None
    error_rows = []

    for index, address in enumerate(unique_addresses, start=1):
        coords, error = geocode_address(address)

        if coords:
            cache[address] = coords
            successes += 1
        elif error == "NOT_FOUND":
            not_found += 1
        else:
            failures += 1
            if first_error is None:
                first_error = error
            if len(error_rows) < 8:
                error_rows.append({"주소": address, "오류": error_message(error)})

            # 인증/권한/요청 한도 오류는 계속 요청해도 해결되지 않으므로 중단
            if error in ("AUTH", "LIMIT") or (error and error.startswith("PERMISSION:")):
                break

        if index % 10 == 0:
            try:
                save_coordinate_cache(cache)
            except Exception as save_error:
                st.warning(f"중간 저장에 실패했습니다: {save_error}")

        progress.progress(index / len(unique_addresses))
        status.write(
            f"변환 중 {index:,}/{len(unique_addresses):,} · "
            f"성공 {successes:,} · 검색 결과 없음 {not_found:,} · 오류 {failures:,}"
        )

        # API에 요청을 과도하게 빠르게 보내지 않도록 간격 유지
        time.sleep(0.15)

    try:
        save_coordinate_cache(cache)
    except Exception as save_error:
        st.error(f"좌표 파일 저장 실패: {save_error}")

    progress.empty()
    status.empty()

    st.success(
        f"변환 완료 · 성공 {successes:,}개 · "
        f"검색 결과 없음 {not_found:,}개 · 오류 {failures:,}개"
    )

    if first_error:
        st.warning(error_message(first_error))

    if error_rows:
        with st.expander("실패한 주소 확인"):
            st.dataframe(
                pd.DataFrame(error_rows),
                use_container_width=True,
                hide_index=True,
            )

    return cache


# =========================================================
# 5. 운영 상태 및 거리 계산
# =========================================================

def get_open_status(row):
    detail = clean_text(row.get("개방시간상세", ""))
    if not detail:
        return "unknown"

    compact = detail.replace(" ", "")
    if any(word in compact for word in ("24시간", "상시개방", "연중무휴", "24시개방")):
        return "open"

    matches = re.findall(
        r"(\d{1,2}:\d{2})\s*[~\-–∼]\s*(\d{1,2}:\d{2})",
        detail,
    )
    if not matches:
        return "unknown"

    now = datetime.now(SEOUL_TZ).time()

    for start_text, end_text in matches:
        try:
            sh, sm = map(int, start_text.split(":"))
            eh, em = map(int, end_text.split(":"))
            if sh > 23 or eh > 23 or sm > 59 or em > 59:
                continue

            start = datetime.strptime(start_text, "%H:%M").time()
            end = datetime.strptime(end_text, "%H:%M").time()

            if start > end:
                if now >= start or now <= end:
                    return "open"
            elif start <= now <= end:
                return "open"
        except ValueError:
            continue

    return "closed"


def marker_color(status):
    return {"open": "green", "closed": "gray"}.get(status, "orange")


def add_distances(df, lat, lon):
    if df.empty:
        return df.copy()

    result = df.copy()
    lats = pd.to_numeric(result["위도"], errors="coerce").astype(float)
    lons = pd.to_numeric(result["경도"], errors="coerce").astype(float)

    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    lat2 = lats.map(math.radians)
    lon2 = lons.map(math.radians)

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        (dlat / 2).map(lambda value: math.sin(value) ** 2)
        + math.cos(lat1)
        * lat2.map(math.cos)
        * (dlon / 2).map(lambda value: math.sin(value) ** 2)
    )
    result["거리(km)"] = 6371 * 2 * a.map(
        lambda value: math.asin(math.sqrt(min(1, max(0, value))))
    )
    return result


# =========================================================
# 6. 지도 팝업 및 지도 만들기
# =========================================================

def popup_html(row):
    address = get_address(row) or "정보 없음"
    hours = clean_text(row.get("개방시간상세")) or "정보 없음"
    owner = clean_text(row.get("화장실소유구분명")) or "정보 없음"

    return f"""
    <div style="width:245px;font-size:13px;line-height:1.65;word-break:break-all;">
        <b>주소</b><br>{safe_html(address)}
        <hr style="margin:7px 0;">
        <b>개방시간상세</b><br>{safe_html(hours)}
        <hr style="margin:7px 0;">
        <b>화장실소유</b><br>{safe_html(owner)}
    </div>
    """


def build_map(rows, center_lat, center_lon, zoom, user_location=None):
    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=zoom,
        control_scale=True,
        tiles="OpenStreetMap",
    )

    if user_location:
        folium.Marker(
            [user_location[0], user_location[1]],
            tooltip="내 위치",
            popup="내 위치",
            icon=folium.Icon(color="blue", icon="user", prefix="fa"),
        ).add_to(m)

        folium.Circle(
            [user_location[0], user_location[1]],
            radius=SEARCH_RADIUS_KM * 1000,
            color="#3182ce",
            fill=False,
            weight=1,
        ).add_to(m)

    cluster = MarkerCluster(
        name="공중화장실",
        options={"maxClusterRadius": 45},
    ).add_to(m)

    for _, row in rows.iterrows():
        try:
            lat = float(row["위도"])
            lon = float(row["경도"])
        except (TypeError, ValueError):
            continue

        status = get_open_status(row)
        folium.Marker(
            location=[lat, lon],
            tooltip="공중화장실",
            popup=folium.Popup(popup_html(row), max_width=290),
            icon=folium.Icon(
                color=marker_color(status),
                icon="info-sign",
            ),
        ).add_to(cluster)

    folium.LayerControl().add_to(m)
    return m


# =========================================================
# 7. 데이터 불러오기
# =========================================================

try:
    if not DATA_PATH.exists():
        raise FileNotFoundError("data/공중화장실정보.csv 파일이 없습니다.")
    toilet_df = load_csv()
except Exception as error:
    st.error(f"데이터를 불러오지 못했습니다: {error}")
    st.info("GitHub 저장소의 data 폴더 안에 CSV 파일이 있는지 확인해 주세요.")
    st.stop()

required_columns = [
    "소재지도로명주소",
    "소재지지번주소",
    "개방시간상세",
    "화장실소유구분명",
]
missing = [column for column in required_columns if column not in toilet_df.columns]
if missing:
    st.error("CSV에 필요한 열이 없습니다: " + ", ".join(missing))
    st.stop()

toilet_df["주소"] = toilet_df.apply(get_address, axis=1)

coordinate_cache = load_coordinate_cache()
toilet_df["위도"] = toilet_df["주소"].map(
    lambda address: coordinate_cache.get(address, (None, None))[0]
)
toilet_df["경도"] = toilet_df["주소"].map(
    lambda address: coordinate_cache.get(address, (None, None))[1]
)

toilet_df["위도"] = pd.to_numeric(toilet_df["위도"], errors="coerce")
toilet_df["경도"] = pd.to_numeric(toilet_df["경도"], errors="coerce")

mapped_df = toilet_df.dropna(subset=["위도", "경도"]).copy()


# =========================================================
# 8. 검색 및 현재 위치
# =========================================================

st.markdown('<div class="section-title">어디의 화장실을 찾으세요?</div>', unsafe_allow_html=True)

if "active_query" not in st.session_state:
    st.session_state["active_query"] = ""

with st.form("search_form", clear_on_submit=False):
    query_input = st.text_input(
        "지역 또는 장소",
        value=st.session_state["active_query"],
        placeholder="예: 경기도, 성남시, 판교역, 강남역",
        label_visibility="collapsed",
    )
    search_clicked = st.form_submit_button("🔎 검색하기", use_container_width=True)

if search_clicked:
    st.session_state["active_query"] = query_input.strip()

button_col, location_col = st.columns([1, 3])
with button_col:
    if st.button("📍 내 주변 보기", use_container_width=True):
        st.session_state["active_query"] = ""
        st.rerun()

with location_col:
    st.markdown(
        '<div class="hint">검색어를 비우면 현재 위치 주변을 표시합니다. 위치 권한을 허용하지 않으면 강남역을 기준으로 합니다.</div>',
        unsafe_allow_html=True,
    )

# 위치 조회가 실패해도 전체 앱이 멈추지 않도록 예외 처리
user_lat = None
user_lon = None
try:
    location_data = streamlit_geolocation()
    if isinstance(location_data, dict):
        raw_lat = location_data.get("latitude")
        raw_lon = location_data.get("longitude")
        if raw_lat is not None and raw_lon is not None:
            user_lat = float(raw_lat)
            user_lon = float(raw_lon)
except Exception:
    user_lat = None
    user_lon = None

query = st.session_state["active_query"].strip()

if user_lat is not None and user_lon is not None:
    default_lat, default_lon = user_lat, user_lon
    location_label = "현재 위치"
    user_marker = (user_lat, user_lon)
else:
    default_lat, default_lon = GANGNAM_LAT, GANGNAM_LON
    location_label = "강남역 기본 위치"
    user_marker = None


# =========================================================
# 9. 검색 결과 만들기
# =========================================================

result_df = pd.DataFrame()
map_rows = pd.DataFrame()
map_lat, map_lon = default_lat, default_lon
map_zoom = 13
search_description = location_label
search_error = None

if query:
    address_mask = (
        toilet_df["소재지도로명주소"].astype(str).str.contains(query, case=False, regex=False, na=False)
        | toilet_df["소재지지번주소"].astype(str).str.contains(query, case=False, regex=False, na=False)
    )
    region_df = toilet_df[address_mask].copy()

    if not region_df.empty:
        result_df = region_df
        map_rows = mapped_df[mapped_df["주소"].isin(region_df["주소"])].copy()

        if not map_rows.empty:
            map_lat = float(map_rows["위도"].mean())
            map_lon = float(map_rows["경도"].mean())
        else:
            # 주소가 검색되었지만 아직 좌표가 없으면 검색어 자체의 좌표로 지도 중심 설정
            coords, error = geocode_address(query) if KAKAO_API_KEY else (None, "NO_KEY")
            if coords:
                map_lat, map_lon = coords
            else:
                search_error = error
        map_zoom = 9
        search_description = f"{query} 검색 결과"
    else:
        # 주소 문자열에서 찾지 못한 경우 장소 검색
        coords, error = search_place(query) if KAKAO_API_KEY else (None, "NO_KEY")
        if coords:
            map_lat, map_lon = coords
            map_zoom = 14
            nearby = add_distances(mapped_df, map_lat, map_lon)
            result_df = nearby[nearby["거리(km)"] <= SEARCH_RADIUS_KM].copy()
            result_df = result_df.sort_values("거리(km)")
            map_rows = result_df.copy()
            search_description = f"{query} 주변 {SEARCH_RADIUS_KM}km"
        else:
            search_error = error
            search_description = f"{query} 검색"
else:
    if not mapped_df.empty:
        nearby = add_distances(mapped_df, default_lat, default_lon)
        result_df = nearby[nearby["거리(km)"] <= SEARCH_RADIUS_KM].copy()
        result_df = result_df.sort_values("거리(km)")
        map_rows = result_df.copy()
    search_description = f"{location_label} 주변 {SEARCH_RADIUS_KM}km"


# =========================================================
# 10. 결과 요약 및 지도 - 좌표가 없어도 지도는 표시
# =========================================================

st.divider()
st.markdown(f'<div class="section-title">📍 {html.escape(search_description)}</div>', unsafe_allow_html=True)

if search_error and result_df.empty:
    st.info(error_message(search_error))

if query and result_df.empty and search_error is None:
    st.info("검색 결과가 없습니다. 지역명이나 장소명을 다시 확인해 주세요.")

if not query and mapped_df.empty:
    st.info(
        "아직 지도에 표시할 좌표가 준비되지 않았습니다. "
        "관리자 메뉴에서 주소 좌표를 준비하면 화장실 마커가 나타납니다."
    )

if not result_df.empty:
    open_count = sum(get_open_status(row) == "open" for _, row in result_df.iterrows())
    closed_count = sum(get_open_status(row) == "closed" for _, row in result_df.iterrows())
    unknown_count = len(result_df) - open_count - closed_count

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("검색된 화장실", f"{len(result_df):,}개")
    c2.metric("운영 중", f"{open_count:,}개")
    c3.metric("운영 종료", f"{closed_count:,}개")
    c4.metric("운영 정보 불명", f"{unknown_count:,}개")
else:
    c1, c2, c3 = st.columns(3)
    c1.metric("전체 화장실 데이터", f"{len(toilet_df):,}개")
    c2.metric("좌표 준비 완료", f"{len(mapped_df):,}개")
    c3.metric("좌표 준비 대기", f"{max(0, len(toilet_df) - len(mapped_df)):,}개")

st.markdown(
    """
    <div style="background:white;border:1px solid #e7edf2;border-radius:12px;padding:10px 14px;margin:8px 0 12px 0;">
    🟢 운영 중 &nbsp;&nbsp; ⚪ 운영 종료 &nbsp;&nbsp; 🟠 운영 정보 불명 &nbsp;&nbsp; 🔵 내 위치
    </div>
    """,
    unsafe_allow_html=True,
)

toilet_map = build_map(
    map_rows,
    map_lat,
    map_lon,
    map_zoom,
    user_location=user_marker if not query else None,
)

st_folium(
    toilet_map,
    use_container_width=True,
    height=620,
    returned_objects=[],
    key="public_toilet_map",
)


# =========================================================
# 11. 검색 결과 목록 - 요청한 3개 항목만
# =========================================================

st.markdown('<div class="section-title">검색 결과 목록</div>', unsafe_allow_html=True)

if not result_df.empty:
    display_df = pd.DataFrame({
        "주소": result_df.apply(get_address, axis=1),
        "개방시간상세": result_df["개방시간상세"].apply(
            lambda value: clean_text(value) or "정보 없음"
        ),
        "화장실소유": result_df["화장실소유구분명"].apply(
            lambda value: clean_text(value) or "정보 없음"
        ),
    })

    st.dataframe(
        display_df.reset_index(drop=True),
        use_container_width=True,
        hide_index=True,
    )
else:
    st.caption("표시할 검색 결과가 없습니다. 좌표가 준비되거나 검색어를 입력하면 결과가 여기에 표시됩니다.")


# =========================================================
# 12. 관리자 메뉴 - 방문자 화면과 분리
# =========================================================

st.divider()

with st.expander("⚙️ 관리자 메뉴 · 주소 좌표 관리"):
    st.caption(
        "이 기능은 운영자만 사용하세요. 방문자는 이 메뉴를 사용할 필요가 없습니다."
    )

    if not ADMIN_PASSWORD:
        st.warning(
            "Streamlit Secrets에 ADMIN_PASSWORD를 먼저 등록해야 관리자 메뉴를 사용할 수 있습니다."
        )
    else:
        admin_input = st.text_input(
            "관리자 비밀번호",
            type="password",
            key="admin_password_input",
        )

        if admin_input == ADMIN_PASSWORD:
            st.success("관리자 인증 완료")

            total_unique = len({
                clean_text(address)
                for address in toilet_df["주소"]
                if clean_text(address)
            })
            ready_count = len(coordinate_cache)

            a1, a2, a3 = st.columns(3)
            a1.metric("전체 고유 주소", f"{total_unique:,}개")
            a2.metric("좌표 변환 완료", f"{ready_count:,}개")
            a3.metric("변환 대기", f"{max(0, total_unique - ready_count):,}개")

            if st.button("전국 주소 좌표 변환 시작", use_container_width=True):
                all_addresses = toilet_df["주소"].tolist()
                coordinate_cache = convert_addresses(all_addresses, coordinate_cache)
                st.rerun()

            if CACHE_PATH.exists():
                cache_bytes = CACHE_PATH.read_bytes()
                st.download_button(
                    "좌표 캐시 CSV 다운로드",
                    data=cache_bytes,
                    file_name="kakao_coordinate_cache.csv",
                    mime="text/csv",
                    use_container_width=True,
                )
                st.caption(
                    "다운로드한 캐시 파일을 GitHub 저장소의 data 폴더에 올리면 "
                    "앱이 다시 시작되어도 변환 결과를 불러올 수 있습니다."
                )
        elif admin_input:
            st.error("관리자 비밀번호가 일치하지 않습니다.")


# =========================================================
# 13. 하단 안내
# =========================================================

st.markdown(
    """
    <div style="text-align:center;color:#94a3b8;font-size:0.82rem;padding-top:18px;">
        공공데이터 기반 공중화장실 위치 안내 · 운영시간은 실제 현장 상황과 다를 수 있습니다.
    </div>
    """,
    unsafe_allow_html=True,
)
