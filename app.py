import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

# ─── PAGE CONFIG ────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="VN Quant Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ─── CUSTOM CSS ─────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .main { background-color: #0e1117; }
    .metric-card {
        background: #1e2130;
        border-radius: 10px;
        padding: 16px;
        border: 1px solid #2d3149;
    }
    .positive { color: #00d09c; }
    .negative { color: #ff4d6d; }
    .stTabs [data-baseweb="tab"] { font-size: 15px; font-weight: 600; }
</style>
""", unsafe_allow_html=True)

# ─── DATA LAYER ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def load_stock_list():
    try:
        from vnstock import Vnstock
        stock = Vnstock().stock(symbol='VCB', source='VCI')
        listing = stock.listing.symbols_by_exchange()
        hose = listing[listing['exchange'] == 'HOSE']['symbol'].tolist()
        return sorted(hose)
    except Exception:
        pass
    # Fallback: common HOSE blue chips
    return ['ACB','BCM','BID','BVH','CTG','FPT','GAS','GVR','HDB',
            'HPG','MBB','MSN','MWG','PLX','POW','SAB','SSB','SSI',
            'STB','TCB','TPB','VCB','VHM','VIB','VIC','VJC','VNM',
            'VPB','VRE','VHC','KDH','NVL','PDR','DXG','HDG']

@st.cache_data(ttl=3600, show_spinner=False)
def load_price_data(symbols: tuple, start: str, end: str):
    results = {}

    def try_fetch(sym):
        try:
            from vnstock import Vnstock
            stock = Vnstock().stock(symbol=sym, source='VCI')
            df = stock.quote.history(start=start, end=end, interval='1D')
            # Normalize column names
            df.columns = [c.lower() for c in df.columns]
            if 'time' in df.columns:
                df = df.rename(columns={'time': 'date'})
            if 'date' not in df.columns and df.index.name == 'time':
                df = df.reset_index().rename(columns={'time': 'date'})
            df['date'] = pd.to_datetime(df['date'])
            return df.set_index('date').sort_index()
        except Exception:
            pass
        return None

    for sym in symbols:
        df = try_fetch(sym)
        if df is not None and len(df) > 20 and 'close' in df.columns:
            results[sym] = df

    # If vnstock unavailable, generate synthetic data for demo
    if not results:
        st.warning("⚠️ Không kết nối được vnstock — đang dùng data demo để minh họa.", icon="⚠️")
        dates = pd.date_range(start=start, end=end, freq='B')
        for sym in symbols:
            np.random.seed(hash(sym) % 999)
            prices = 50000 * np.exp(np.cumsum(np.random.normal(0.0003, 0.015, len(dates))))
            vol = np.abs(np.random.normal(1e6, 3e5, len(dates)))
            results[sym] = pd.DataFrame({
                'open': prices * np.random.uniform(0.99, 1.01, len(dates)),
                'high': prices * np.random.uniform(1.00, 1.03, len(dates)),
                'low':  prices * np.random.uniform(0.97, 1.00, len(dates)),
                'close': prices,
                'volume': vol
            }, index=dates)
    return results

# ─── QUANT ENGINE ────────────────────────────────────────────────────────────
def compute_returns(price_series: pd.Series) -> pd.Series:
    return price_series.pct_change().dropna()

def compute_log_returns(price_series: pd.Series) -> pd.Series:
    return np.log(price_series / price_series.shift(1)).dropna()

def compute_risk_metrics(returns: pd.Series, trading_days=252) -> dict:
    r = returns.dropna()
    if len(r) < 10:
        return {}

    cumulative = (1 + r).cumprod()
    total_return = cumulative.iloc[-1] - 1
    ann_return = (1 + total_return) ** (trading_days / len(r)) - 1
    ann_vol = r.std() * np.sqrt(trading_days)
    sharpe = (ann_return - 0.045) / ann_vol if ann_vol > 0 else 0  # rf = 4.5% VN

    # Drawdown
    roll_max = cumulative.cummax()
    drawdown = (cumulative - roll_max) / roll_max
    max_dd = drawdown.min()

    # VaR & CVaR (historical, 95%)
    var_95 = np.percentile(r, 5)
    cvar_95 = r[r <= var_95].mean()

    # Sortino
    downside = r[r < 0].std() * np.sqrt(trading_days)
    sortino = (ann_return - 0.045) / downside if downside > 0 else 0

    # Calmar
    calmar = ann_return / abs(max_dd) if max_dd != 0 else 0

    # Win rate
    win_rate = (r > 0).sum() / len(r)

    return {
        'total_return': total_return,
        'ann_return': ann_return,
        'ann_vol': ann_vol,
        'sharpe': sharpe,
        'sortino': sortino,
        'calmar': calmar,
        'max_drawdown': max_dd,
        'var_95': var_95,
        'cvar_95': cvar_95,
        'win_rate': win_rate,
        'drawdown_series': drawdown,
        'cumulative': cumulative,
    }

def detect_market_regime(returns: pd.Series, window=20) -> pd.Series:
    """Simple regime detection: Trend / Neutral / Bear based on rolling stats"""
    roll_ret  = returns.rolling(window).mean() * 252
    roll_vol  = returns.rolling(window).std() * np.sqrt(252)
    roll_z    = (returns - returns.rolling(window).mean()) / returns.rolling(window).std()

    regime = pd.Series('Neutral', index=returns.index)
    regime[roll_ret > 0.15] = 'Bull'
    regime[roll_ret < -0.15] = 'Bear'
    regime[roll_vol > 0.35] = 'High Volatility'
    return regime

def run_backtest(price_data: dict, weights: dict, start: str, end: str) -> dict:
    """Equal or custom weight portfolio backtest"""
    all_returns = {}
    for sym, df in price_data.items():
        if sym in weights:
            r = compute_returns(df['close'])
            all_returns[sym] = r

    if not all_returns:
        return {}

    ret_df = pd.DataFrame(all_returns).dropna()
    w = np.array([weights[s] for s in ret_df.columns])
    w = w / w.sum()  # normalize

    portfolio_returns = ret_df.values @ w
    portfolio_returns = pd.Series(portfolio_returns, index=ret_df.index)

    metrics = compute_risk_metrics(portfolio_returns)
    metrics['weights'] = dict(zip(ret_df.columns, w))
    metrics['daily_returns'] = portfolio_returns
    metrics['individual'] = {
        sym: compute_risk_metrics(compute_returns(price_data[sym]['close']))
        for sym in ret_df.columns
    }
    return metrics

# ─── UI HELPERS ──────────────────────────────────────────────────────────────
def fmt_pct(v, decimals=2):
    sign = "+" if v > 0 else ""
    color = "positive" if v >= 0 else "negative"
    return f'<span class="{color}">{sign}{v*100:.{decimals}f}%</span>'

def metric_box(label, value, sub=None):
    sub_html = f"<div style='font-size:12px;color:#888;margin-top:4px'>{sub}</div>" if sub else ""
    st.markdown(f"""
    <div class="metric-card">
        <div style='font-size:13px;color:#888;margin-bottom:6px'>{label}</div>
        <div style='font-size:22px;font-weight:700'>{value}</div>
        {sub_html}
    </div>""", unsafe_allow_html=True)

# ─── SIDEBAR ────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("⚙️ Cấu hình")
    st.divider()

    end_date   = datetime.today()
    start_date = end_date - timedelta(days=365)

    with st.spinner("Đang tải danh sách mã..."):
        all_symbols = load_stock_list()

    selected_symbols = st.multiselect(
        "Chọn mã chứng khoán",
        options=all_symbols,
        default=all_symbols[:5] if len(all_symbols) >= 5 else all_symbols,
        help="Chọn tối đa 10 mã để app chạy nhanh"
    )

    if len(selected_symbols) > 10:
        st.warning("Nên chọn ≤ 10 mã để đảm bảo tốc độ")

    st.divider()
    weight_mode = st.radio("Phân bổ danh mục", ["Equal Weight", "Custom Weight"])

    weights = {}
    if selected_symbols:
        if weight_mode == "Equal Weight":
            weights = {s: 1/len(selected_symbols) for s in selected_symbols}
        else:
            st.markdown("**Nhập trọng số (%):**")
            total = 0
            for sym in selected_symbols:
                w = st.number_input(sym, min_value=0.0, max_value=100.0,
                                     value=round(100/len(selected_symbols), 1), step=0.5)
                weights[sym] = w / 100
                total += w
            if abs(total - 100) > 0.1:
                st.error(f"Tổng = {total:.1f}% (cần = 100%)")

    run_btn = st.button("🚀 Chạy phân tích", type="primary", width="stretch")

# ─── MAIN ────────────────────────────────────────────────────────────────────
st.title("📊 VN Quant Dashboard")
st.caption(f"Dữ liệu: {start_date.strftime('%d/%m/%Y')} → {end_date.strftime('%d/%m/%Y')} | HOSE")

if not selected_symbols:
    st.info("👈 Chọn ít nhất 1 mã ở sidebar để bắt đầu")
    st.stop()

if run_btn or 'price_data' not in st.session_state:
    with st.spinner(f"Đang tải dữ liệu {len(selected_symbols)} mã..."):
        price_data = load_price_data(
            tuple(selected_symbols),
            start_date.strftime('%Y-%m-%d'),
            end_date.strftime('%Y-%m-%d')
        )
    st.session_state['price_data'] = price_data
    st.session_state['weights'] = weights

price_data = st.session_state.get('price_data', {})
weights    = st.session_state.get('weights', weights)

loaded_syms = list(price_data.keys())
if not loaded_syms:
    st.error("Không tải được dữ liệu. Kiểm tra kết nối vnstock.")
    st.stop()

# ── TABS ─────────────────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4 = st.tabs([
    "📈 Portfolio Backtest",
    "⚠️ Risk Metrics",
    "🌡️ Market Regime",
    "🔍 So sánh mã"
])

# ════════════════════════════════════════════════════════════════════════════
# TAB 1 — PORTFOLIO BACKTEST
# ════════════════════════════════════════════════════════════════════════════
with tab1:
    bt = run_backtest(price_data, {s: weights.get(s, 1) for s in loaded_syms},
                      start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d'))

    if not bt:
        st.warning("Không đủ dữ liệu để backtest")
    else:
        # KPI row
        c1, c2, c3, c4, c5 = st.columns(5)
        with c1: metric_box("Tổng lợi nhuận", f"{bt['total_return']*100:.2f}%")
        with c2: metric_box("Lợi nhuận/năm", f"{bt['ann_return']*100:.2f}%")
        with c3: metric_box("Biến động/năm", f"{bt['ann_vol']*100:.2f}%")
        with c4: metric_box("Sharpe Ratio", f"{bt['sharpe']:.2f}")
        with c5: metric_box("Max Drawdown", f"{bt['max_drawdown']*100:.2f}%")

        st.markdown("---")

        # Equity curve + drawdown
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                            row_heights=[0.7, 0.3],
                            subplot_titles=("Equity Curve", "Drawdown"))

        cumret = bt['cumulative']
        fig.add_trace(go.Scatter(
            x=cumret.index, y=cumret.values,
            name="Portfolio", line=dict(color="#00d09c", width=2),
            fill='tozeroy', fillcolor='rgba(0,208,156,0.05)'
        ), row=1, col=1)

        dd = bt['drawdown_series']
        fig.add_trace(go.Scatter(
            x=dd.index, y=dd.values * 100,
            name="Drawdown %", line=dict(color="#ff4d6d", width=1.5),
            fill='tozeroy', fillcolor='rgba(255,77,109,0.1)'
        ), row=2, col=1)

        fig.update_layout(
            template='plotly_dark', height=520,
            margin=dict(l=0, r=0, t=30, b=0),
            legend=dict(orientation='h', y=1.05),
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)'
        )
        fig.update_yaxes(title_text="Cumulative Return", row=1, col=1)
        fig.update_yaxes(title_text="Drawdown %", row=2, col=1)
        st.plotly_chart(fig, width="stretch")

        # Weight pie
        st.subheader("Phân bổ danh mục")
        w_data = bt['weights']
        fig_pie = go.Figure(go.Pie(
            labels=list(w_data.keys()),
            values=[v*100 for v in w_data.values()],
            hole=0.4,
            marker_colors=px.colors.qualitative.Set3
        ))
        fig_pie.update_layout(
            template='plotly_dark', height=320,
            paper_bgcolor='rgba(0,0,0,0)',
            margin=dict(l=0, r=0, t=0, b=0)
        )
        st.plotly_chart(fig_pie, width="stretch")

# ════════════════════════════════════════════════════════════════════════════
# TAB 2 — RISK METRICS
# ════════════════════════════════════════════════════════════════════════════
with tab2:
    rows = []
    for sym in loaded_syms:
        r = compute_returns(price_data[sym]['close'])
        m = compute_risk_metrics(r)
        if m:
            rows.append({
                'Mã': sym,
                'Return/năm': f"{m['ann_return']*100:.1f}%",
                'Biến động': f"{m['ann_vol']*100:.1f}%",
                'Sharpe': f"{m['sharpe']:.2f}",
                'Sortino': f"{m['sortino']:.2f}",
                'Calmar': f"{m['calmar']:.2f}",
                'Max DD': f"{m['max_drawdown']*100:.1f}%",
                'VaR 95%': f"{m['var_95']*100:.2f}%",
                'CVaR 95%': f"{m['cvar_95']*100:.2f}%",
                'Win Rate': f"{m['win_rate']*100:.1f}%",
                '_ann_return': m['ann_return'],
                '_sharpe': m['sharpe'],
            })

    if rows:
        df_metrics = pd.DataFrame(rows)

        # Scatter: Risk vs Return
        fig_rr = go.Figure()
        for _, row in df_metrics.iterrows():
            vol = float(row['Biến động'].replace('%',''))
            ret = float(row['Return/năm'].replace('%',''))
            fig_rr.add_trace(go.Scatter(
                x=[vol], y=[ret],
                mode='markers+text',
                text=[row['Mã']],
                textposition='top center',
                marker=dict(size=14, color='#7c83fd',
                            line=dict(color='white', width=1)),
                name=row['Mã'],
                showlegend=False
            ))

        fig_rr.add_hline(y=0, line_dash='dash', line_color='gray', opacity=0.5)
        fig_rr.update_layout(
            template='plotly_dark', height=400,
            title='Risk vs Return (mỗi mã)',
            xaxis_title='Biến động/năm (%)',
            yaxis_title='Return/năm (%)',
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(14,17,23,0.8)'
        )
        st.plotly_chart(fig_rr, width="stretch")

        # Table
        st.subheader("Bảng chi tiết Risk Metrics")
        display_cols = ['Mã','Return/năm','Biến động','Sharpe','Sortino',
                        'Calmar','Max DD','VaR 95%','CVaR 95%','Win Rate']
        st.dataframe(
            df_metrics[display_cols],
            width="stretch",
            hide_index=True
        )

        # Correlation heatmap
        st.subheader("Tương quan giữa các mã")
        ret_matrix = pd.DataFrame({
            sym: compute_returns(price_data[sym]['close'])
            for sym in loaded_syms
        }).dropna()
        corr = ret_matrix.corr()

        fig_heat = go.Figure(go.Heatmap(
            z=corr.values,
            x=corr.columns.tolist(),
            y=corr.index.tolist(),
            colorscale='RdBu_r',
            zmid=0,
            text=np.round(corr.values, 2),
            texttemplate='%{text}',
            textfont=dict(size=11),
        ))
        fig_heat.update_layout(
            template='plotly_dark', height=420,
            paper_bgcolor='rgba(0,0,0,0)',
            margin=dict(l=0, r=0, t=10, b=0)
        )
        st.plotly_chart(fig_heat, width="stretch")

# ════════════════════════════════════════════════════════════════════════════
# TAB 3 — MARKET REGIME
# ════════════════════════════════════════════════════════════════════════════
with tab3:
    st.subheader("Market Regime Detection")
    st.caption("Phân loại chế độ thị trường dựa trên rolling return & volatility (20 ngày)")

    regime_sym = st.selectbox("Chọn mã để phân tích regime", loaded_syms)

    df_sym  = price_data[regime_sym]
    returns = compute_returns(df_sym['close'])
    regime  = detect_market_regime(returns)

    # Color map
    color_map = {
        'Bull': '#00d09c',
        'Bear': '#ff4d6d',
        'Neutral': '#7c83fd',
        'High Volatility': '#ffd700'
    }

    # Price chart with regime background
    fig_reg = go.Figure()

    # Regime background bands
    prev_reg = None
    start_idx = None
    for i, (date, reg) in enumerate(regime.items()):
        if reg != prev_reg:
            if prev_reg is not None:
                fig_reg.add_vrect(
                    x0=start_idx, x1=date,
                    fillcolor=color_map.get(prev_reg, 'gray'),
                    opacity=0.12, line_width=0
                )
            start_idx = date
            prev_reg = reg
    if start_idx:
        fig_reg.add_vrect(
            x0=start_idx, x1=regime.index[-1],
            fillcolor=color_map.get(prev_reg, 'gray'),
            opacity=0.12, line_width=0
        )

    # Price line
    fig_reg.add_trace(go.Scatter(
        x=df_sym.index, y=df_sym['close'],
        name=regime_sym,
        line=dict(color='white', width=1.5)
    ))

    fig_reg.update_layout(
        template='plotly_dark', height=420,
        title=f'{regime_sym} — Giá & Regime',
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(14,17,23,0.8)',
        margin=dict(l=0, r=0, t=40, b=0)
    )
    st.plotly_chart(fig_reg, width="stretch")

    # Regime distribution
    regime_counts = regime.value_counts()
    fig_reg_pie = go.Figure(go.Bar(
        x=regime_counts.index.tolist(),
        y=regime_counts.values,
        marker_color=[color_map.get(r, 'gray') for r in regime_counts.index]
    ))
    fig_reg_pie.update_layout(
        template='plotly_dark', height=280,
        title='Phân phối Regime (số ngày)',
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(14,17,23,0.8)',
        margin=dict(l=0, r=0, t=40, b=0)
    )
    st.plotly_chart(fig_reg_pie, width="stretch")

    # Current regime
    current_regime = regime.iloc[-1] if len(regime) > 0 else "N/A"
    color = color_map.get(current_regime, 'white')
    st.markdown(f"""
    <div class="metric-card" style="text-align:center; padding: 24px;">
        <div style='font-size:14px;color:#888;margin-bottom:8px'>Regime hiện tại</div>
        <div style='font-size:32px;font-weight:800;color:{color}'>{current_regime}</div>
    </div>
    """, unsafe_allow_html=True)

# ════════════════════════════════════════════════════════════════════════════
# TAB 4 — SO SÁNH MÃ
# ════════════════════════════════════════════════════════════════════════════
with tab4:
    st.subheader("So sánh hiệu suất các mã")

    # Normalized price (base = 100)
    fig_cmp = go.Figure()
    colors = px.colors.qualitative.Set3

    for i, sym in enumerate(loaded_syms):
        close = price_data[sym]['close'].dropna()
        if len(close) > 0:
            normalized = close / close.iloc[0] * 100
            fig_cmp.add_trace(go.Scatter(
                x=normalized.index,
                y=normalized.values,
                name=sym,
                line=dict(width=1.8, color=colors[i % len(colors)])
            ))

    fig_cmp.add_hline(y=100, line_dash='dash', line_color='gray', opacity=0.4)
    fig_cmp.update_layout(
        template='plotly_dark', height=450,
        title='Hiệu suất chuẩn hóa (base = 100)',
        yaxis_title='Giá trị (base 100)',
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(14,17,23,0.8)',
        margin=dict(l=0, r=0, t=40, b=0),
        legend=dict(orientation='h', y=-0.15)
    )
    st.plotly_chart(fig_cmp, width="stretch")

    # Rolling volatility comparison
    st.subheader("Rolling Volatility (20 ngày)")
    fig_vol = go.Figure()
    for i, sym in enumerate(loaded_syms):
        r = compute_returns(price_data[sym]['close'])
        roll_vol = r.rolling(20).std() * np.sqrt(252) * 100
        fig_vol.add_trace(go.Scatter(
            x=roll_vol.index, y=roll_vol.values,
            name=sym,
            line=dict(width=1.5, color=colors[i % len(colors)])
        ))

    fig_vol.update_layout(
        template='plotly_dark', height=350,
        yaxis_title='Annualized Vol (%)',
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(14,17,23,0.8)',
        margin=dict(l=0, r=0, t=10, b=0),
        legend=dict(orientation='h', y=-0.2)
    )
    st.plotly_chart(fig_vol, width="stretch")

# ─── FOOTER ──────────────────────────────────────────────────────────────────
st.divider()
st.caption("VN Quant Dashboard · Dữ liệu từ vnstock (HOSE/HNX) · Chỉ mang tính tham khảo, không phải khuyến nghị đầu tư")