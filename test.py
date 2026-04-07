import os
import json
import pandas as pd
from datetime import datetime
from groq import Groq
import streamlit as st

# 1. CẤU HÌNH TRANG
st.set_page_config(page_title="Student Review Analyzer", page_icon="🎓", layout="wide")

# Tên file version mới để chứa thêm cột Từ Khóa
FILE_NAME = "reviews_history_v4.csv"

# --- SIDEBAR: CẤU HÌNH & QUẢN LÝ ---
with st.sidebar:
    st.header("⚙️ Cài đặt hệ thống")
    api_key = st.text_input("Nhập Groq API Key của bạn:", type="password")
    
    st.divider()
    st.subheader("🗑️ Quản lý dữ liệu")
    if st.button("Xóa toàn bộ lịch sử", type="primary"):
        if os.path.exists(FILE_NAME):
            os.remove(FILE_NAME)
            st.success("Đã xóa sạch dữ liệu cũ!")
            st.rerun()

# --- GIAO DIỆN CHÍNH: NHẬP & PHÂN TÍCH ---
st.title("🎓 Student Review Analyzer Pro Max")
st.markdown("*Công cụ bóc tách, trích xuất từ khóa và phân tích đánh giá môn học*")

review = st.text_area(
    "✍️ Nhập review môn học:", 
    placeholder="VD: Môn Cấu trúc dữ liệu thầy dạy kỹ, demo code C++ trực quan dễ hiểu nhưng đồ án cuối kỳ hơi khoai, deadline dí liên tục...", 
    height=130
)

if st.button("🚀 Phân tích ngay", use_container_width=True):
    if not api_key:
        st.error("⚠️ Bạn chưa nhập Groq API Key ở thanh công cụ bên trái!")
    elif not review:
        st.warning("⚠️ Vui lòng nhập nội dung review trước khi phân tích!")
    else:
        with st.spinner("🤖 AI đang đọc, bóc tách và tìm kiếm từ khóa..."):
            try:
                client = Groq(api_key=api_key)
                
                # Bổ sung yêu cầu trích xuất "Từ khóa" vào prompt
                prompt = f"""Bạn là chuyên gia phân tích dữ liệu giáo dục. Hãy phân tích review sau.
                BẮT BUỘC trả về ĐÚNG định dạng JSON dưới đây, KHÔNG giải thích thêm:
                {{
                    "mon_hoc": "Tên môn học (hoặc 'Không xác định')",
                    "sentiment": "Tích cực" hoặc "Tiêu cực" hoặc "Trung lập",
                    "diem_tot": "Liệt kê chi tiết điểm tốt (hoặc 'Không có')",
                    "diem_chua_tot": "Liệt kê chi tiết điểm chưa tốt (hoặc 'Không có')",
                    "tom_tat": "1 câu tóm tắt ngắn gọn nhất",
                    "tu_khoa": "Tối đa 3 từ khóa nổi bật nhất, cách nhau bằng dấu phẩy (VD: deadline, thực hành, thầy hiền)"
                }}
                
                Review: "{review}"
                """
                
                response = client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1 
                )
                
                ai_result = response.choices[0].message.content.strip()
                if ai_result.startswith("```json"):
                    ai_result = ai_result[7:-3]
                elif ai_result.startswith("```"):
                    ai_result = ai_result[3:-3]
                    
                parsed_data = json.loads(ai_result.strip())
                
                # Giao diện hiển thị kết quả
                st.success("✅ Phân tích hoàn tất!")
                col1, col2 = st.columns(2)
                with col1:
                    st.info(f"**📚 Môn học:** {parsed_data.get('mon_hoc')}")
                    st.info(f"**🎯 Sentiment:** {parsed_data.get('sentiment')}")
                    st.info(f"**🔑 Từ khóa:** {parsed_data.get('tu_khoa')}")
                with col2:
                    st.success(f"**👍 Điểm tốt:** {parsed_data.get('diem_tot')}")
                    st.error(f"**👎 Điểm chưa tốt:** {parsed_data.get('diem_chua_tot')}")
                
                st.warning(f"**📝 Tóm tắt:** {parsed_data.get('tom_tat')}")
                
                # Lưu Data
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                new_data = pd.DataFrame({
                    "Thời gian": [current_time],
                    "Môn học": [parsed_data.get("mon_hoc")],
                    "Review": [review],
                    "Sentiment": [parsed_data.get("sentiment")],
                    "Điểm tốt": [parsed_data.get("diem_tot")],
                    "Điểm chưa tốt": [parsed_data.get("diem_chua_tot")],
                    "Từ khóa": [parsed_data.get("tu_khoa")]
                })
                
                if not os.path.exists(FILE_NAME):
                    new_data.to_csv(FILE_NAME, mode='w', header=True, index=False, encoding='utf-8-sig')
                else:
                    new_data.to_csv(FILE_NAME, mode='a', header=False, index=False, encoding='utf-8-sig')
                    
            except json.JSONDecodeError:
                st.error("❌ AI trả về sai định dạng JSON. Bạn thử ấn Phân tích lại nhé!")
                st.code(ai_result) 
            except Exception as e:
                st.error(f"❌ Có lỗi xảy ra: {e}")

# --- DASHBOARD & THỐNG KÊ PRO MAX ---
st.divider()
st.subheader("📊 Dashboard Thống Kê & Quản Lý")

if os.path.exists(FILE_NAME):
    df = pd.read_csv(FILE_NAME)
    
    if not df.empty:
        # BỘ LỌC THÔNG MINH
        st.markdown("### 🔍 Bộ lọc môn học")
        danh_sach_mon = ["Tất cả"] + df['Môn học'].unique().tolist()
        mon_duoc_chon = st.selectbox("Chọn môn học để xem thống kê riêng:", danh_sach_mon)
        
        # Lọc dataframe theo môn được chọn
        if mon_duoc_chon != "Tất cả":
            df_filtered = df[df['Môn học'] == mon_duoc_chon]
        else:
            df_filtered = df
            
        # 1. METRICS (Hiển thị theo data đã lọc)
        m1, m2, m3, m4 = st.columns(4)
        m1.metric(f"Số Review ({mon_duoc_chon})", len(df_filtered))
        m2.metric("Tích cực 😁", len(df_filtered[df_filtered['Sentiment'] == 'Tích cực']))
        m3.metric("Tiêu cực 😡", len(df_filtered[df_filtered['Sentiment'] == 'Tiêu cực']))
        m4.metric("Trung lập 😐", len(df_filtered[df_filtered['Sentiment'] == 'Trung lập']))
        
        st.write("---")
        
        # 2. BIỂU ĐỒ KÉP (Sentiment và Keyword)
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("##### 📈 Tỷ lệ Cảm xúc")
            sentiment_counts = df_filtered['Sentiment'].value_counts()
            if not sentiment_counts.empty:
                st.bar_chart(sentiment_counts)
            else:
                st.info("Chưa có dữ liệu cảm xúc")
                
        with c2:
            st.markdown("##### 🏷️ Top Từ khóa sinh viên hay nhắc đến")
            # Tách các từ khóa được ngăn cách bằng dấu phẩy và đếm số lần xuất hiện
            if 'Từ khóa' in df_filtered.columns and not df_filtered['Từ khóa'].dropna().empty:
                all_keywords = df_filtered['Từ khóa'].dropna().str.split(',').explode().str.strip()
                top_kw = all_keywords.value_counts().head(5)
                if not top_kw.empty:
                    st.bar_chart(top_kw)
                else:
                    st.info("Chưa trích xuất được từ khóa")
            else:
                st.info("Chưa có dữ liệu từ khóa")
                
        st.write("---")
        
        # 3. QUẢN LÝ DATA TRỰC TIẾP (Excel-like Interface)
        st.markdown("##### 🗄️ Quản lý Dữ liệu gốc (Click đúp để sửa, tick ô vuông để xóa)")
        
        # Sử dụng data_editor thay vì dataframe để cho phép chỉnh sửa trực tiếp
        edited_df = st.data_editor(df, num_rows="dynamic", use_container_width=True, key="data_editor")
        
        c3, c4 = st.columns([1, 4])
        with c3:
            # Nút lưu thay đổi từ data editor xuống file CSV
            if st.button("💾 Lưu thay đổi bảng", type="primary"):
                edited_df.to_csv(FILE_NAME, index=False, encoding='utf-8-sig')
                st.success("Đã lưu data thành công!")
                st.rerun() # Tải lại trang để cập nhật biểu đồ
                
        with c4:
            csv = df.to_csv(index=False).encode('utf-8-sig')
            st.download_button(label="📥 Tải file CSV", data=csv, file_name='reviews_export_v4.csv', mime='text/csv')
    else:
        st.info("File dữ liệu đang trống. Hãy phân tích thử một review nhé!")
else:
    st.info("Chưa có dữ liệu. Hãy phân tích thử một review để mở khóa Dashboard nhé!")