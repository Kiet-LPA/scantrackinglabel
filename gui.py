import streamlit as st
import pandas as pd
import time
from sheet_logger import init_log

def display_ui():
    st.set_page_config(page_title="Quản lý xuất kho - Tracking", layout="wide")
    st.title("📦 Quản lý xuất kho - Tracking")
    placeholder = st.empty()

    while True:
        df = pd.read_excel("log.xlsx")
        with placeholder.container():
            st.dataframe(df, use_container_width=True)
        time.sleep(2)

if __name__ == "__main__":
    init_log()
    display_ui()

