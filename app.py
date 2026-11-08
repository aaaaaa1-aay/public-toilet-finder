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
