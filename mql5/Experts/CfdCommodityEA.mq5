//+------------------------------------------------------------------+
//| CfdCommodityEA.mq5                                               |
//| 商品CFD（原油・銀）向け自動売買 EA                                  |
//|                                                                  |
//| Python 側 (cfdbot) と同じルールで動くように作ってある:              |
//|  - 指標: EMA/RMA は最初の値で初期化、標準偏差は母標準偏差          |
//|  - シグナルは確定足で判断し、新しい足の最初のティックで執行         |
//|  - 逆指値は証券会社側に置き、足の確定時にだけ書き換える            |
//|                                                                  |
//| 1チャート = 1銘柄 × 1戦略。同じ銘柄で複数戦略を動かす場合は          |
//| ea_magic を変えて別チャートに貼る（同一銘柄の同時保有は              |
//| rk_max_positions_per_symbol まで。既定 1）。                        |
//| ea_magic の上位桁 (ea_magic / 1000) が同じ EA 同士でリスクを合算。  |
//|                                                                  |
//| ※ MetaEditor でコンパイル・デモ口座で動作確認してから使うこと。      |
//+------------------------------------------------------------------+
#property copyright "cfdbot"
#property version   "0.10"
#property description "ATR-based commodity CFD EA (Donchian / Squeeze / Pullback / Reversion)"

#include <Trade\Trade.mqh>

enum ENUM_CFD_STRATEGY
  {
   STRAT_DONCHIAN  = 0, // A: ドンチャン・ブレイクアウト（原油の主力）
   STRAT_SQUEEZE   = 1, // B: スクイーズ・ブレイクアウト（銀の主力）
   STRAT_PULLBACK  = 2, // C: トレンド押し目/戻り
   STRAT_REVERSION = 3  // D: レンジ逆張り（補助）
  };

enum ENUM_SERVER_TZ
  {
   TZ_NY_CLOSE = 0, // サーバー時刻 = 米東部時間+7h（冬GMT+2/夏GMT+3）
   TZ_FIXED    = 1  // 固定オフセット（ea_server_offset）
  };

//--- 戦略 --------------------------------------------------------------
input group "戦略"
input ENUM_CFD_STRATEGY strategy = STRAT_DONCHIAN; // 戦略

input group "A: ドンチャン"
input int    dc_entry_period = 40;    // エントリーチャネル本数
input int    dc_exit_period  = 20;    // 手仕舞いチャネル本数
input int    dc_trend_ema    = 200;   // トレンドフィルタEMA（0=無効）
input double dc_buffer_atr   = 0.0;   // ブレイク幅の下限（ATR倍）

input group "B: スクイーズ"
input int    sq_bb_period        = 20;
input double sq_bb_mult          = 2.0;
input double sq_kc_mult          = 1.5;
input int    sq_box_period       = 20;
input int    sq_min_squeeze_bars = 6;
input double sq_stop_box_frac    = 0.5;

input group "C: 押し目/戻り"
input int    pb_fast_ema        = 20;
input int    pb_slow_ema        = 100;
input int    pb_slope_bars      = 5;
input int    pb_adx_period      = 14;
input double pb_adx_min         = 20.0;
input double pb_touch_atr       = 0.5;
input int    pb_setup_bars      = 3;
input int    pb_swing_bars      = 5;
input double pb_stop_buffer_atr = 0.5;

input group "D: レンジ逆張り"
input int    mr_period     = 20;
input double mr_z_entry    = 2.0;
input double mr_exit_z     = 0.0;
input int    mr_rsi_period = 2;
input double mr_rsi_low    = 10.0;
input double mr_rsi_high   = 90.0;
input int    mr_adx_period = 14;
input double mr_adx_max    = 20.0;

input group "上位足フィルタ"
input int    htf_minutes = 0;   // 上位足（分。240=H4, 1440=D1）。0=使わない
input int    htf_ema     = 0;   // 上位足の終値が EMA より上なら買いだけ、下なら売りだけ

//--- 出口 --------------------------------------------------------------
input group "出口管理"
input int    ex_atr_period             = 20;
input double ex_init_stop_atr          = 2.5;  // 初期損切り（ATR倍）
input double ex_min_stop_atr           = 1.0;
input double ex_max_stop_atr           = 4.0;
input double ex_breakeven_trigger_atr  = 1.0;  // 建値移動の条件（0=無効）
input double ex_breakeven_offset_atr   = 0.1;
input double ex_trail_atr              = 3.0;  // シャンデリア（0=無効）
input double ex_min_update_atr         = 0.1;
input double ex_partial_tp_r           = 0.0;  // 一部利確 +xR（0=無効）
input double ex_partial_fraction       = 0.5;
input int    ex_time_stop_bars         = 0;
input double ex_time_stop_min_r        = 0.5;
input int    ex_max_hold_bars          = 0;
input bool   ex_flatten_before_weekend = false;
input bool   ex_flatten_before_events  = false;

//--- リスク ------------------------------------------------------------
input group "リスク管理"
input double rk_risk_per_trade           = 0.01; // 1回の損失（資産比）
input double rk_max_total_risk           = 0.03; // 全ポジション合計
input int    rk_cluster_id               = 0;    // 1=エネルギー, 2=貴金属, 0=なし
input double rk_cluster_max_risk         = 0.015;
input int    rk_max_positions_per_symbol = 1;
input double rk_daily_loss_limit         = 0.03;
input double rk_max_drawdown_halt        = 0.25;
input double rk_max_margin_utilization   = 0.5;
input double rk_max_leverage_symbol      = 1.0;  // 1銘柄のレバレッジ上限（名目÷資産、0=無効）
input double rk_max_leverage_total       = 2.0;  // 全銘柄合計のレバレッジ上限（0=無効）
input double rk_min_lot_overshoot        = 1.0;
input double rk_max_lots                 = 0.0;  // 自主上限ロット（0=証券会社上限のみ）

//--- フィルタ ----------------------------------------------------------
input group "フィルタ"
input bool   ft_oil_events             = true;  // API(火16:30ET)/EIA(水10:30ET)
input string ft_event_tags             = "oil"; // CSVイベントの対象タグ（カンマ区切り）
input int    ft_event_block_before_min = 240;
input int    ft_event_block_after_min  = 60;
input double ft_no_entry_after_fri_et  = 12.0;  // 金曜この時刻(ET)以降は新規停止（<0で無効）
input double ft_weekend_flatten_fri_et = 16.0;
input int    ft_max_spread_points      = 0;     // 0=チェックしない

//--- EA 固有 -----------------------------------------------------------
input group "EA"
input long           ea_magic            = 2609001;
input string         ea_param_file       = "";      // Common\Files の key=value（空=inputのみ）
input int            ea_param_reload_min = 60;
input string         ea_events_file      = "cfdbot_events.csv"; // Common\Files（無ければ無視）
input ENUM_SERVER_TZ ea_server_tz        = TZ_NY_CLOSE;
input double         ea_server_offset    = 2.0;     // TZ_FIXED のときのUTCからの時差
input int            ea_calc_bars        = 4000;    // 指標計算に使う本数（長い EMA ほど多く必要）
input int            ea_timeframe_minutes = 0;      // 学習した時間足（分）。違うチャートでは起動しない（0=確認しない）
input int            ea_spread_wait_min  = 0;       // スプレッド拡大時に待つ分数（0=見送り）
input int            ea_deviation_points = 50;
input bool           ea_push_notify      = true;    // MT5 プッシュ通知（iPhone）
input string         ea_webhook_url      = "";      // n8n 等（ツール→オプション→EAで許可が必要）
input bool           ea_log_signals      = false;   // シグナルを CSV に記録（Python と突き合わせ用）

//+------------------------------------------------------------------+
struct Params
  {
   int    strategy;
   int    dc_entry_period, dc_exit_period, dc_trend_ema;
   double dc_buffer_atr;
   int    sq_bb_period, sq_box_period, sq_min_squeeze_bars;
   double sq_bb_mult, sq_kc_mult, sq_stop_box_frac;
   int    pb_fast_ema, pb_slow_ema, pb_slope_bars, pb_adx_period, pb_setup_bars, pb_swing_bars;
   double pb_adx_min, pb_touch_atr, pb_stop_buffer_atr;
   int    mr_period, mr_rsi_period, mr_adx_period;
   int    htf_minutes, htf_ema;
   double mr_z_entry, mr_exit_z, mr_rsi_low, mr_rsi_high, mr_adx_max;
   int    ex_atr_period, ex_time_stop_bars, ex_max_hold_bars;
   double ex_init_stop_atr, ex_min_stop_atr, ex_max_stop_atr, ex_breakeven_trigger_atr;
   double ex_breakeven_offset_atr, ex_trail_atr, ex_min_update_atr, ex_partial_tp_r;
   double ex_partial_fraction, ex_time_stop_min_r;
   bool   ex_flatten_before_weekend, ex_flatten_before_events;
   double rk_risk_per_trade, rk_max_total_risk, rk_cluster_max_risk, rk_daily_loss_limit;
   double rk_max_drawdown_halt, rk_max_margin_utilization, rk_min_lot_overshoot, rk_max_lots;
   double rk_max_leverage_symbol, rk_max_leverage_total;
   int    rk_cluster_id, rk_max_positions_per_symbol;
   bool   ft_oil_events;
   string ft_event_tags;
   int    ft_event_block_before_min, ft_event_block_after_min, ft_max_spread_points;
   double ft_no_entry_after_fri_et, ft_weekend_flatten_fri_et;
  };

struct CfdSignal
  {
   int    entry;       // +1 / -1 / 0
   double stop_dist;   // <0 なら既定（ATR倍）
   bool   exit_long;
   bool   exit_short;
  };

struct CfdEvent
  {
   datetime et;        // 米東部時間の壁時計
   string   name;
   string   tag;
  };

CTrade   trade;
Params   P;
CfdEvent g_events[];
string   g_tags[];

MqlRates g_rates[];
int      g_n = 0;
double   g_o[], g_h[], g_l[], g_c[], g_atr[];
datetime g_t[];

datetime g_last_reload = 0;
datetime g_last_checked = 0;  // 最後に確認した形成中の足の時刻
long     g_group = 0;
string   g_gv = "";          // この EA 用のグローバル変数接頭辞
string   g_gv_group = "";    // 同じグループ（口座レベル）の接頭辞

// スプレッド待ちの予約
bool     g_pend = false;
CfdSignal g_pend_sig;
double   g_pend_atr = 0;
datetime g_pend_tc = 0;
datetime g_pend_expire = 0;

//+------------------------------------------------------------------+
int OnInit()
  {
   if(ea_timeframe_minutes > 0 && PeriodSeconds(_Period) != ea_timeframe_minutes * 60)
     {
      Alert(StringFormat("CfdCommodityEA: このパラメータは %d 分足用。チャートの時間足を合わせること", ea_timeframe_minutes));
      return(INIT_PARAMETERS_INCORRECT);
     }
   LoadDefaults();
   if(ea_param_file != "")
      LoadParamFile(ea_param_file);
   LoadEvents();
   g_group = ea_magic / 1000;
   g_gv = "cfdbot." + IntegerToString(ea_magic) + ".";
   g_gv_group = "cfdbot.g" + IntegerToString(g_group) + ".";
   GlobalVariableSet("cfdbot.cid." + _Symbol, P.rk_cluster_id);
   trade.SetExpertMagicNumber((ulong)ea_magic);
   trade.SetDeviationInPoints((ulong)ea_deviation_points);
   trade.SetTypeFillingBySymbol(_Symbol);
   EventSetTimer(60);
   PrintFormat("CfdCommodityEA start %s strategy=%d magic=%s", _Symbol, P.strategy, IntegerToString(ea_magic));
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

void OnTimer()
  {
   if(ea_param_file != "" && TimeCurrent() - g_last_reload >= ea_param_reload_min * 60)
     {
      LoadParamFile(ea_param_file);
      GlobalVariableSet("cfdbot.cid." + _Symbol, P.rk_cluster_id);
     }
  }

void OnTick()
  {
   UpdateAccountGuards();
   CheckPartialTakeProfit();
   if(g_pend)
      TryPendingEntry();

   datetime cur = iTime(_Symbol, _Period, 0);
   if(cur == 0 || cur == g_last_checked)
      return;
   if(!LoadBars())
      return;
   g_last_checked = cur;
   int k = g_n - 1;
   datetime last_done = (datetime)GVGet(g_gv + "lastbar", 0);
   if(g_t[k] <= last_done)
      return;  // 再起動時などに同じ足を二重に処理しない
   GlobalVariableSet(g_gv + "lastbar", (double)g_t[k]);
   OnNewBar(k);
  }

//+------------------------------------------------------------------+
//| 確定足の処理                                                       |
//+------------------------------------------------------------------+
void OnNewBar(const int k)
  {
   datetime tc = g_t[k] + PeriodSeconds(_Period);   // 確定足の終了時刻（サーバー時刻）
   CfdSignal s;
   ComputeSignal(k, s);
   if(ea_log_signals)
      LogSignal(k, s);
   ManagePositions(k, tc, s);
   CleanupPositionGVs();
   g_pend = false;
   if(s.entry != 0)
     {
      // 停止中・遅延した足では新規を出さない
      if(TimeCurrent() - tc > 2 * PeriodSeconds(_Period))
        {
         Print("stale bar, skip entry");
         return;
        }
      TryEntry(s, g_atr[k], tc);
     }
  }

//+------------------------------------------------------------------+
//| データと指標                                                       |
//+------------------------------------------------------------------+
bool LoadBars()
  {
   int n = CopyRates(_Symbol, _Period, 1, ea_calc_bars, g_rates);
   if(n < 100)
     {
      Print("not enough bars: ", n);
      return(false);
     }
   g_n = n;
   ArrayResize(g_o, n);
   ArrayResize(g_h, n);
   ArrayResize(g_l, n);
   ArrayResize(g_c, n);
   ArrayResize(g_t, n);
   for(int i = 0; i < n; i++)
     {
      g_o[i] = g_rates[i].open;
      g_h[i] = g_rates[i].high;
      g_l[i] = g_rates[i].low;
      g_c[i] = g_rates[i].close;
      g_t[i] = g_rates[i].time;
     }
   CalcATR(P.ex_atr_period, g_atr);
   return(true);
  }

void CalcEMA(const double &src[], const int period, double &out[])
  {
   int n = ArraySize(src);
   ArrayResize(out, n);
   double a = 2.0 / (period + 1.0);
   out[0] = src[0];
   for(int i = 1; i < n; i++)
      out[i] = out[i - 1] + a * (src[i] - out[i - 1]);
  }

void CalcRMA(const double &src[], const int period, double &out[])
  {
   int n = ArraySize(src);
   ArrayResize(out, n);
   double a = 1.0 / period;
   out[0] = src[0];
   for(int i = 1; i < n; i++)
      out[i] = out[i - 1] + a * (src[i] - out[i - 1]);
  }

void CalcTR(double &tr[])
  {
   ArrayResize(tr, g_n);
   tr[0] = g_h[0] - g_l[0];
   for(int i = 1; i < g_n; i++)
      tr[i] = MathMax(g_h[i] - g_l[i], MathMax(MathAbs(g_h[i] - g_c[i - 1]), MathAbs(g_l[i] - g_c[i - 1])));
  }

void CalcATR(const int period, double &out[])
  {
   double tr[];
   CalcTR(tr);
   CalcRMA(tr, period, out);
  }

void CalcADX(const int period, double &out[])
  {
   double tr[], pdm[], mdm[], str[], spdm[], smdm[], dx[];
   CalcTR(tr);
   ArrayResize(pdm, g_n);
   ArrayResize(mdm, g_n);
   pdm[0] = 0;
   mdm[0] = 0;
   for(int i = 1; i < g_n; i++)
     {
      double up = g_h[i] - g_h[i - 1];
      double dn = g_l[i - 1] - g_l[i];
      pdm[i] = (up > dn && up > 0) ? up : 0.0;
      mdm[i] = (dn > up && dn > 0) ? dn : 0.0;
     }
   CalcRMA(tr, period, str);
   CalcRMA(pdm, period, spdm);
   CalcRMA(mdm, period, smdm);
   ArrayResize(dx, g_n);
   for(int i = 0; i < g_n; i++)
     {
      double pdi = str[i] > 0 ? 100.0 * spdm[i] / str[i] : 0.0;
      double mdi = str[i] > 0 ? 100.0 * smdm[i] / str[i] : 0.0;
      double sum = pdi + mdi;
      dx[i] = sum > 0 ? 100.0 * MathAbs(pdi - mdi) / sum : 0.0;
     }
   CalcRMA(dx, period, out);
  }

void CalcRSI(const int period, double &out[])
  {
   double up[], dn[], su[], sd[];
   ArrayResize(up, g_n);
   ArrayResize(dn, g_n);
   up[0] = 0;
   dn[0] = 0;
   for(int i = 1; i < g_n; i++)
     {
      double d = g_c[i] - g_c[i - 1];
      up[i] = d > 0 ? d : 0.0;
      dn[i] = d < 0 ? -d : 0.0;
     }
   CalcRMA(up, period, su);
   CalcRMA(dn, period, sd);
   ArrayResize(out, g_n);
   for(int i = 0; i < g_n; i++)
     {
      if(sd[i] > 0)
         out[i] = 100.0 - 100.0 / (1.0 + su[i] / sd[i]);
      else
         out[i] = su[i] > 0 ? 100.0 : 50.0;
     }
  }

double SMAAt(const double &src[], const int i, const int period)
  {
   double s = 0;
   for(int j = i - period + 1; j <= i; j++)
      s += src[j];
   return(s / period);
  }

double StdAt(const double &src[], const int i, const int period)
  {
   double m = SMAAt(src, i, period);
   double s = 0;
   for(int j = i - period + 1; j <= i; j++)
      s += (src[j] - m) * (src[j] - m);
   return(MathSqrt(s / period));
  }

double HighestHigh(const int from, const int to) // [from, to]
  {
   double v = g_h[from];
   for(int i = from + 1; i <= to; i++)
      v = MathMax(v, g_h[i]);
   return(v);
  }

double LowestLow(const int from, const int to)
  {
   double v = g_l[from];
   for(int i = from + 1; i <= to; i++)
      v = MathMin(v, g_l[i]);
   return(v);
  }

//+------------------------------------------------------------------+
//| シグナル（Python の strategies/*.py と同じ式）                       |
//+------------------------------------------------------------------+
void ComputeSignal(const int k, CfdSignal &s)
  {
   s.entry = 0;
   s.stop_dist = -1;
   s.exit_long = false;
   s.exit_short = false;
   bool lg = false, sh = false;
   double stop_l = -1, stop_s = -1;
   double atr = g_atr[k];

   if(P.strategy == STRAT_DONCHIAN)
     {
      int N = P.dc_entry_period, M = P.dc_exit_period;
      if(k < N)
         return;
      double upper = HighestHigh(k - N, k - 1);
      double lower = LowestLow(k - N, k - 1);
      double buf = P.dc_buffer_atr * atr;
      lg = g_c[k] > upper + buf;
      sh = g_c[k] < lower - buf;
      if(P.dc_trend_ema > 0)
        {
         double e[];
         CalcEMA(g_c, P.dc_trend_ema, e);
         lg = lg && g_c[k] > e[k];
         sh = sh && g_c[k] < e[k];
        }
      s.exit_long = g_c[k] < LowestLow(k - M, k - 1);
      s.exit_short = g_c[k] > HighestHigh(k - M, k - 1);
     }
   else
      if(P.strategy == STRAT_SQUEEZE)
        {
         int B = P.sq_box_period;
         if(k < B || k - B < P.sq_bb_period - 1)
            return;
         int cnt = 0;
         for(int i = k - B; i <= k - 1; i++)
            if(P.sq_bb_mult * StdAt(g_c, i, P.sq_bb_period) < P.sq_kc_mult * g_atr[i])
               cnt++;
         double bh = HighestHigh(k - B, k - 1);
         double bl = LowestLow(k - B, k - 1);
         bool armed = cnt >= P.sq_min_squeeze_bars;
         lg = armed && g_c[k] > bh;
         sh = armed && g_c[k] < bl;
         double height = bh - bl;
         stop_l = g_c[k] - (bh - P.sq_stop_box_frac * height);
         stop_s = (bl + P.sq_stop_box_frac * height) - g_c[k];
        }
      else
         if(P.strategy == STRAT_PULLBACK)
           {
            if(k < MathMax(P.pb_slope_bars, MathMax(P.pb_setup_bars, P.pb_swing_bars)))
               return;
            double fast[], slow[], adx[];
            CalcEMA(g_c, P.pb_fast_ema, fast);
            CalcEMA(g_c, P.pb_slow_ema, slow);
            CalcADX(P.pb_adx_period, adx);
            bool strong = adx[k] >= P.pb_adx_min;
            bool up = fast[k] > slow[k] && slow[k] > slow[k - P.pb_slope_bars] && g_c[k] > slow[k] && strong;
            bool dn = fast[k] < slow[k] && slow[k] < slow[k - P.pb_slope_bars] && g_c[k] < slow[k] && strong;
            bool setup_l = false, setup_s = false;
            for(int i = k - P.pb_setup_bars + 1; i <= k; i++)
              {
               if(g_l[i] <= fast[i] + P.pb_touch_atr * g_atr[i])
                  setup_l = true;
               if(g_h[i] >= fast[i] - P.pb_touch_atr * g_atr[i])
                  setup_s = true;
              }
            lg = up && setup_l && g_c[k] > g_h[k - 1];
            sh = dn && setup_s && g_c[k] < g_l[k - 1];
            double swing_low = LowestLow(k - P.pb_swing_bars + 1, k);
            double swing_high = HighestHigh(k - P.pb_swing_bars + 1, k);
            stop_l = g_c[k] - (swing_low - P.pb_stop_buffer_atr * atr);
            stop_s = (swing_high + P.pb_stop_buffer_atr * atr) - g_c[k];
            s.exit_long = fast[k] < slow[k];
            s.exit_short = fast[k] > slow[k];
           }
         else
            if(P.strategy == STRAT_REVERSION)
              {
               if(k < P.mr_period - 1)
                  return;
               double adx[], rsi[];
               CalcADX(P.mr_adx_period, adx);
               CalcRSI(P.mr_rsi_period, rsi);
               double mid = SMAAt(g_c, k, P.mr_period);
               double sd = StdAt(g_c, k, P.mr_period);
               if(sd <= 0)
                  return;
               double z = (g_c[k] - mid) / sd;
               bool ranging = adx[k] < P.mr_adx_max;
               lg = ranging && z < -P.mr_z_entry && rsi[k] < P.mr_rsi_low;
               sh = ranging && z > P.mr_z_entry && rsi[k] > P.mr_rsi_high;
               s.exit_long = z >= P.mr_exit_z;
               s.exit_short = z <= -P.mr_exit_z;
              }

   if(lg && !sh)
     {
      s.entry = 1;
      s.stop_dist = stop_l;
     }
   else
      if(sh && !lg)
        {
         s.entry = -1;
         s.stop_dist = stop_s;
        }
   // 上位足フィルタ（Python の Sleeve.htf_* と同じ: 確定済みの上位足の終値と EMA を比べる）
   if(s.entry != 0 && P.htf_minutes > 0 && P.htf_ema > 0)
     {
      int trend = HigherTimeframeTrend();
      if((s.entry > 0 && trend <= 0) || (s.entry < 0 && trend >= 0))
        {
         s.entry = 0;
         s.stop_dist = -1;
        }
     }
  }

ENUM_TIMEFRAMES MinutesToTimeframe(const int m)
  {
   switch(m)
     {
      case 1:
         return(PERIOD_M1);
      case 5:
         return(PERIOD_M5);
      case 10:
         return(PERIOD_M10);
      case 15:
         return(PERIOD_M15);
      case 30:
         return(PERIOD_M30);
      case 60:
         return(PERIOD_H1);
      case 240:
         return(PERIOD_H4);
      case 1440:
         return(PERIOD_D1);
     }
   return(PERIOD_CURRENT);
  }

// +1: 上位足の終値 > EMA、-1: 終値 < EMA、0: 判定できない（データ不足など。この場合は新規を出さない）
int HigherTimeframeTrend()
  {
   ENUM_TIMEFRAMES tf = MinutesToTimeframe(P.htf_minutes);
   if(tf == PERIOD_CURRENT || PeriodSeconds(tf) <= PeriodSeconds(_Period))
      return(0);
   MqlRates r[];
   int n = CopyRates(_Symbol, tf, 1, MathMax(P.htf_ema * 4, 300), r);  // 1 = 確定済みの足から
   if(n < P.htf_ema)
      return(0);
   double a = 2.0 / (P.htf_ema + 1.0);
   double e = r[0].close;
   for(int i = 1; i < n; i++)
      e = e + a * (r[i].close - e);
   double c = r[n - 1].close;
   return(c > e ? 1 : (c < e ? -1 : 0));
  }

double ClipStop(double stop_dist, const double atr)
  {
   if(stop_dist <= 0)
      stop_dist = P.ex_init_stop_atr * atr;
   return(MathMin(MathMax(stop_dist, P.ex_min_stop_atr * atr), P.ex_max_stop_atr * atr));
  }

//+------------------------------------------------------------------+
//| 保有ポジションの管理（exits.py の on_bar_close と同じ）               |
//+------------------------------------------------------------------+
void ManagePositions(const int k, const datetime tc, const CfdSignal &s)
  {
   double tf = PeriodSeconds(_Period);
   double spread = CurrentSpread();
   double stop_level = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;
   datetime tc_et = ServerToET(tc);
   for(int p = PositionsTotal() - 1; p >= 0; p--)
     {
      ulong ticket = PositionGetTicket(p);
      if(ticket == 0 || PositionGetString(POSITION_SYMBOL) != _Symbol || PositionGetInteger(POSITION_MAGIC) != ea_magic)
         continue;
      int side = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? 1 : -1;
      double entry = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl = PositionGetDouble(POSITION_SL);
      double tp = PositionGetDouble(POSITION_TP);
      datetime ptime = (datetime)PositionGetInteger(POSITION_TIME);
      if(ptime >= g_t[k] + (int)tf)
         continue;  // 形成中の足で建てたものは次の確定足から管理
      int e = BarIndexOf(ptime);
      string pk = PosKey(ticket);
      if(!GlobalVariableCheck(pk + "r"))  // 記録が無い（手動建て・再起動など）→ 現在の逆指値から復元
        {
         GlobalVariableSet(pk + "r", sl > 0 ? MathAbs(entry - sl) : ClipStop(-1, g_atr[k]));
         GlobalVariableSet(pk + "a", g_atr[e > 0 ? e - 1 : 0]);
        }
      double r_dist = GlobalVariableGet(pk + "r");
      double atr_entry = GlobalVariableGet(pk + "a");
      bool be_done = GVGet(pk + "be", 0) > 0;

      // 週末・指標前の手仕舞い
      if(P.ex_flatten_before_weekend &&
         (FridayCutoffPassed(tc_et, P.ft_weekend_flatten_fri_et) || FridayCutoffPassed(tc_et + (int)tf, P.ft_weekend_flatten_fri_et)))
        {
         ClosePosition(ticket, "weekend");
         continue;
        }
      if(P.ex_flatten_before_events && EventUpcoming(tc_et, (int)tf))
        {
         ClosePosition(ticket, "event");
         continue;
        }

      if((side > 0 && s.exit_long) || (side < 0 && s.exit_short))
        {
         ClosePosition(ticket, "signal");
         continue;
        }

      int bars_held = k - e + 1;
      double extreme = side > 0 ? HighestHigh(e, k) : LowestLow(e, k);
      double close = g_c[k];
      double atr = g_atr[k];
      if(P.ex_max_hold_bars > 0 && bars_held >= P.ex_max_hold_bars)
        {
         ClosePosition(ticket, "max_hold");
         continue;
        }
      if(P.ex_time_stop_bars > 0 && bars_held >= P.ex_time_stop_bars && r_dist > 0)
        {
         double mark = side > 0 ? close : close + spread;
         if(side * (mark - entry) / r_dist < P.ex_time_stop_min_r)
           {
            ClosePosition(ticket, "time_stop");
            continue;
           }
        }

      double cand = sl;
      if(P.ex_breakeven_trigger_atr > 0 && !be_done)
        {
         double mark = side > 0 ? close : close + spread;
         if(side * (mark - entry) >= P.ex_breakeven_trigger_atr * atr_entry)
           {
            double be = entry + side * P.ex_breakeven_offset_atr * atr_entry;
            cand = Better(side, cand, be);
            be_done = true;
            GlobalVariableSet(pk + "be", 1);
           }
        }
      if(P.ex_trail_atr > 0)
        {
         double trail = side > 0 ? extreme - P.ex_trail_atr * atr : extreme + P.ex_trail_atr * atr + spread;
         cand = Better(side, cand, trail);
        }

      if(cand != sl)
        {
         if((side > 0 && cand >= close - stop_level) || (side < 0 && cand <= close + spread + stop_level))
           {
            ClosePosition(ticket, "stop_level");
            continue;
           }
         bool moved_be = be_done && side * (cand - entry) >= 0 && side * (sl - entry) < 0;
         if(MathAbs(cand - sl) >= P.ex_min_update_atr * atr || moved_be || sl == 0)
            ModifyStop(ticket, side, cand, tp);
        }
     }
  }

double Better(const int side, const double a, const double b)
  {
   if(a == 0)
      return(b);
   return(side > 0 ? MathMax(a, b) : MathMin(a, b));
  }

int BarIndexOf(const datetime t)
  {
   // g_t は昇順。t を含む足（g_t[i] <= t）の最後の添字
   int lo = 0, hi = g_n - 1, ans = 0;
   while(lo <= hi)
     {
      int mid = (lo + hi) / 2;
      if(g_t[mid] <= t)
        {
         ans = mid;
         lo = mid + 1;
        }
      else
         hi = mid - 1;
     }
   return(ans);
  }

void CheckPartialTakeProfit()
  {
   if(P.ex_partial_tp_r <= 0)
      return;
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
      return;
   for(int p = PositionsTotal() - 1; p >= 0; p--)
     {
      ulong ticket = PositionGetTicket(p);
      if(ticket == 0 || PositionGetString(POSITION_SYMBOL) != _Symbol || PositionGetInteger(POSITION_MAGIC) != ea_magic)
         continue;
      string pk = PosKey(ticket);
      if(GVGet(pk + "pt", 0) > 0 || !GlobalVariableCheck(pk + "r"))
         continue;
      int side = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? 1 : -1;
      double entry = PositionGetDouble(POSITION_PRICE_OPEN);
      double tp = entry + side * P.ex_partial_tp_r * GlobalVariableGet(pk + "r");
      bool hit = side > 0 ? tick.bid >= tp : tick.ask <= tp;
      if(!hit)
         continue;
      GlobalVariableSet(pk + "pt", 1);
      double vol = PositionGetDouble(POSITION_VOLUME);
      double part = NormalizeVolumeDown(vol * P.ex_partial_fraction);
      double vmin = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
      if(part < vmin || vol - part < vmin - 1e-12)
         continue;  // 最小ロットの都合で分割できない
      if(ClosePartial(ticket, side, part))
         Notify(StringFormat("%s 一部利確 %.2f lots @%s", _Symbol, part, DoubleToString(side > 0 ? tick.bid : tick.ask, _Digits)));
     }
  }

bool ClosePartial(const ulong ticket, const int side, const double part)
  {
   if(AccountInfoInteger(ACCOUNT_MARGIN_MODE) == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
      return(trade.PositionClosePartial(ticket, part));
   // ネッティング口座: 反対売買で数量を減らす（逆指値はそのまま残る）
   return(side > 0 ? trade.Sell(part, _Symbol, 0, 0, 0, "cfdbot partial") : trade.Buy(part, _Symbol, 0, 0, 0, "cfdbot partial"));
  }

//+------------------------------------------------------------------+
//| 新規エントリー                                                     |
//+------------------------------------------------------------------+
void TryEntry(const CfdSignal &s, const double atr, const datetime tc)
  {
   string why = EntryBlockReason(tc);
   if(why != "")
     {
      PrintFormat("entry %d rejected: %s", s.entry, why);
      return;
     }
   if(P.ft_max_spread_points > 0 && SymbolInfoInteger(_Symbol, SYMBOL_SPREAD) > P.ft_max_spread_points)
     {
      if(ea_spread_wait_min > 0)
        {
         g_pend = true;
         g_pend_sig = s;
         g_pend_atr = atr;
         g_pend_tc = tc;
         g_pend_expire = TimeCurrent() + ea_spread_wait_min * 60;
         Print("spread too wide, waiting");
        }
      else
         Print("entry rejected: spread");
      return;
     }
   ExecuteEntry(s, atr);
  }

void TryPendingEntry()
  {
   if(TimeCurrent() > g_pend_expire)
     {
      g_pend = false;
      Print("pending entry expired (spread)");
      return;
     }
   if(SymbolInfoInteger(_Symbol, SYMBOL_SPREAD) > P.ft_max_spread_points)
      return;
   g_pend = false;
   if(EntryBlockReason(g_pend_tc) == "")
      ExecuteEntry(g_pend_sig, g_pend_atr);
  }

string EntryBlockReason(const datetime tc)
  {
   if(GVGet(g_gv_group + "halt", 0) > 0)
      return("halt");
   if(DailyLossBlocked())
      return("daily_loss");
   datetime et = ServerToET(tc);
   if(P.ft_no_entry_after_fri_et >= 0 && FridayCutoffPassed(et, P.ft_no_entry_after_fri_et))
      return("weekend");
   if(EventInWindow(et, P.ft_event_block_before_min * 60, P.ft_event_block_after_min * 60))
      return("event");
   if(CountGroupPositions(_Symbol) >= P.rk_max_positions_per_symbol)
      return("symbol_limit");
   if(GVGet(g_gv + "lastentry", 0) >= (double)tc)
      return("duplicate");
   return("");
  }

void ExecuteEntry(const CfdSignal &s, const double atr)
  {
   int side = s.entry;
   double spread = CurrentSpread();
   double stop_level = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;
   double stop_dist = ClipStop(s.stop_dist, atr);
   stop_dist = MathMax(stop_dist, stop_level + spread + _Point);

   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double heat = 0, cheat = 0;
   GroupHeat(heat, cheat);
   double budget = P.rk_max_total_risk * equity - heat;
   if(P.rk_cluster_id > 0)
      budget = MathMin(budget, P.rk_cluster_max_risk * equity - cheat);
   if(budget <= 0)
     {
      Print("entry rejected: heat");
      return;
     }
   double target = MathMin(equity * P.rk_risk_per_trade, budget);
   double loss_per_lot = LossPerLot(_Symbol, stop_dist);
   if(loss_per_lot <= 0)
     {
      Print("entry rejected: tick value unavailable");
      return;
     }
   double vmin = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double lots = NormalizeVolumeDown(target / loss_per_lot);
   lots = MathMin(lots, MaxLotsAllowed());
   lots = NormalizeVolumeDown(lots);
   if(lots < vmin)
     {
      if(vmin * loss_per_lot <= target * P.rk_min_lot_overshoot && vmin <= MaxLotsAllowed())
         lots = vmin;
      else
        {
         PrintFormat("entry rejected: min_lot (1lot risk=%.0f, target=%.0f)", vmin * loss_per_lot, target);
         return;
        }
     }
   ENUM_ORDER_TYPE type = side > 0 ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   double price = side > 0 ? SymbolInfoDouble(_Symbol, SYMBOL_ASK) : SymbolInfoDouble(_Symbol, SYMBOL_BID);
   // レバレッジ上限（名目建玉 ÷ 資産）。超える分は数量を減らす
   double cap_lots = LeverageCapLots(equity);
   if(cap_lots >= 0 && lots > cap_lots)
     {
      lots = NormalizeVolumeDown(cap_lots);
      if(lots < vmin)
        {
         PrintFormat("entry rejected: leverage (cap=%.2f lots)", cap_lots);
         return;
        }
     }
   // 証拠金チェック（証券会社が証拠金率を変えても自動で追従する）
   double margin = 0, margin1 = 0;
   double avail = P.rk_max_margin_utilization * equity - AccountInfoDouble(ACCOUNT_MARGIN);
   if(OrderCalcMargin(type, _Symbol, lots, price, margin) && margin > avail)
     {
      if(!OrderCalcMargin(type, _Symbol, 1.0, price, margin1) || margin1 <= 0 || avail <= 0)
        {
         Print("entry rejected: margin");
         return;
        }
      lots = NormalizeVolumeDown(avail / margin1);
      if(lots < vmin)
        {
         Print("entry rejected: margin");
         return;
        }
     }
   SendEntry(side, lots, stop_dist, atr);
  }

void SendEntry(const int side, double lots, const double stop_dist, const double atr)
  {
   for(int attempt = 0; attempt < 3; attempt++)
     {
      MqlTick tick;
      if(!SymbolInfoTick(_Symbol, tick))
         return;
      double price = side > 0 ? tick.ask : tick.bid;
      double sl = NormalizePrice(price - side * stop_dist);
      bool ok = side > 0 ? trade.Buy(lots, _Symbol, price, sl, 0, "cfdbot") : trade.Sell(lots, _Symbol, price, sl, 0, "cfdbot");
      uint rc = trade.ResultRetcode();
      if(ok && (rc == TRADE_RETCODE_DONE || rc == TRADE_RETCODE_PLACED))
        {
         GlobalVariableSet(g_gv + "lastentry", GVGet(g_gv + "lastbar", 0) + PeriodSeconds(_Period));
         RegisterNewPosition(stop_dist, atr);
         Notify(StringFormat("%s %s %.2f lots @%s SL=%s", _Symbol, side > 0 ? "BUY" : "SELL", lots,
                             DoubleToString(trade.ResultPrice(), _Digits), DoubleToString(sl, _Digits)));
         return;
        }
      PrintFormat("order failed rc=%u %s", rc, trade.ResultRetcodeDescription());
      if(rc == TRADE_RETCODE_REQUOTE || rc == TRADE_RETCODE_PRICE_CHANGED || rc == TRADE_RETCODE_PRICE_OFF ||
         rc == TRADE_RETCODE_TIMEOUT || rc == TRADE_RETCODE_CONNECTION)
        {
         Sleep(500);
         continue;
        }
      if(rc == TRADE_RETCODE_NO_MONEY || rc == TRADE_RETCODE_INVALID_VOLUME || rc == TRADE_RETCODE_LIMIT_VOLUME ||
         rc == TRADE_RETCODE_LIMIT_POSITIONS)
        {
         // 証券会社のルール変更（証拠金率・建玉上限）に備え、半分の数量で1回だけ再試行
         double vmin = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
         double half = NormalizeVolumeDown(lots / 2.0);
         Notify(StringFormat("%s 発注拒否 rc=%u (%s) lots=%.2f", _Symbol, rc, trade.ResultRetcodeDescription(), lots));
         if(attempt == 0 && half >= vmin)
           {
            lots = half;
            continue;
           }
        }
      return;
     }
  }

void RegisterNewPosition(const double stop_dist, const double atr)
  {
   for(int p = PositionsTotal() - 1; p >= 0; p--)
     {
      ulong ticket = PositionGetTicket(p);
      if(ticket == 0 || PositionGetString(POSITION_SYMBOL) != _Symbol || PositionGetInteger(POSITION_MAGIC) != ea_magic)
         continue;
      string pk = PosKey(ticket);
      if(GlobalVariableCheck(pk + "r"))
         continue;
      GlobalVariableSet(pk + "r", stop_dist);
      GlobalVariableSet(pk + "a", atr);
      GlobalVariableSet(pk + "be", 0);
      GlobalVariableSet(pk + "pt", 0);
     }
  }

void ModifyStop(const ulong ticket, const int side, double new_sl, const double tp)
  {
   new_sl = NormalizePrice(new_sl);
   double freeze = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_FREEZE_LEVEL) * _Point;
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double stops = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;
   if((side > 0 && new_sl >= bid - MathMax(stops, freeze)) || (side < 0 && new_sl <= ask + MathMax(stops, freeze)))
     {
      ClosePosition(ticket, "stop_level");
      return;
     }
   if(!trade.PositionModify(ticket, new_sl, tp))
      PrintFormat("modify failed rc=%u %s", trade.ResultRetcode(), trade.ResultRetcodeDescription());
  }

void ClosePosition(const ulong ticket, const string reason)
  {
   if(trade.PositionClose(ticket))
      Notify(StringFormat("%s 決済 (%s)", _Symbol, reason));
   else
      Notify(StringFormat("%s 決済失敗 (%s) rc=%u", _Symbol, reason, trade.ResultRetcode()));
  }

//+------------------------------------------------------------------+
//| リスク集計（同じグループの EA 全体）                                 |
//+------------------------------------------------------------------+
bool IsGroupPosition()
  {
   return(PositionGetInteger(POSITION_MAGIC) / 1000 == g_group);
  }

int CountGroupPositions(const string sym)
  {
   int n = 0;
   for(int p = PositionsTotal() - 1; p >= 0; p--)
     {
      ulong ticket = PositionGetTicket(p);
      if(ticket != 0 && PositionGetString(POSITION_SYMBOL) == sym && IsGroupPosition())
         n++;
     }
   return(n);
  }

void GroupHeat(double &heat, double &cluster_heat)
  {
   heat = 0;
   cluster_heat = 0;
   for(int p = PositionsTotal() - 1; p >= 0; p--)
     {
      ulong ticket = PositionGetTicket(p);
      if(ticket == 0 || !IsGroupPosition())
         continue;
      string sym = PositionGetString(POSITION_SYMBOL);
      int side = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? 1 : -1;
      double entry = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl = PositionGetDouble(POSITION_SL);
      double vol = PositionGetDouble(POSITION_VOLUME);
      double r;
      if(sl <= 0)
         r = AccountInfoDouble(ACCOUNT_EQUITY) * P.rk_risk_per_trade;  // 逆指値なし → 1回分とみなす
      else
         r = LossPerLot(sym, MathMax(0.0, side * (entry - sl))) * vol;
      heat += r;
      if(P.rk_cluster_id > 0 && (int)GVGet("cfdbot.cid." + sym, -1) == P.rk_cluster_id)
         cluster_heat += r;
     }
  }

double LossPerLot(const string sym, const double price_dist)
  {
   double tick_size = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   double tick_value = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE_LOSS);
   if(tick_size <= 0 || tick_value <= 0)
      return(0);
   return(price_dist / tick_size * tick_value);
  }

// 1ロットの名目建玉（口座通貨）。価格 ÷ ティックサイズ × ティック価値
double NotionalPerLot(const string sym)
  {
   double tick_size = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   double tick_value = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE_PROFIT);
   double price = SymbolInfoDouble(sym, SYMBOL_BID);
   if(tick_size <= 0 || tick_value <= 0 || price <= 0)
      return(0);
   return(price / tick_size * tick_value);
  }

// レバレッジ上限まで、この銘柄をあと何ロット持てるか（上限なしなら -1）
double LeverageCapLots(const double equity)
  {
   if(P.rk_max_leverage_symbol <= 0 && P.rk_max_leverage_total <= 0)
      return(-1);
   double sym_notional = 0, total_notional = 0;
   for(int p = PositionsTotal() - 1; p >= 0; p--)
     {
      ulong ticket = PositionGetTicket(p);
      if(ticket == 0 || !IsGroupPosition())
         continue;
      string sym = PositionGetString(POSITION_SYMBOL);
      double n = NotionalPerLot(sym) * PositionGetDouble(POSITION_VOLUME);
      total_notional += n;
      if(sym == _Symbol)
         sym_notional += n;
     }
   double room = DBL_MAX;
   if(P.rk_max_leverage_symbol > 0)
      room = MathMin(room, P.rk_max_leverage_symbol * equity - sym_notional);
   if(P.rk_max_leverage_total > 0)
      room = MathMin(room, P.rk_max_leverage_total * equity - total_notional);
   double per_lot = NotionalPerLot(_Symbol);
   if(per_lot <= 0)
      return(-1);
   return(MathMax(room, 0.0) / per_lot);
  }

double MaxLotsAllowed()
  {
   double vmax = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double vlimit = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_LIMIT);
   if(vlimit > 0)
     {
      double held = 0;
      for(int p = PositionsTotal() - 1; p >= 0; p--)
        {
         ulong ticket = PositionGetTicket(p);
         if(ticket != 0 && PositionGetString(POSITION_SYMBOL) == _Symbol)
            held += PositionGetDouble(POSITION_VOLUME);
        }
      vmax = MathMin(vmax, vlimit - held);
     }
   if(P.rk_max_lots > 0)
      vmax = MathMin(vmax, P.rk_max_lots);
   return(MathMax(vmax, 0));
  }

void UpdateAccountGuards()
  {
   double eq = AccountInfoDouble(ACCOUNT_EQUITY);
   datetime et = ServerToET(TimeTradeServer());
   double day_key = (double)TradingDayKey(et);
   if(GVGet(g_gv_group + "daykey", -1) != day_key)
     {
      GlobalVariableSet(g_gv_group + "daykey", day_key);
      GlobalVariableSet(g_gv_group + "daystart", eq);
     }
   double peak = MathMax(GVGet(g_gv_group + "peak", eq), eq);
   GlobalVariableSet(g_gv_group + "peak", peak);
   if(GVGet(g_gv_group + "halt", 0) == 0 && eq <= peak * (1.0 - P.rk_max_drawdown_halt))
     {
      GlobalVariableSet(g_gv_group + "halt", 1);
      Notify(StringFormat("最大DD到達のため新規停止（equity=%.0f peak=%.0f）。解除は GV %shalt を削除", eq, peak, g_gv_group));
     }
  }

bool DailyLossBlocked()
  {
   double start = GVGet(g_gv_group + "daystart", 0);
   return(start > 0 && AccountInfoDouble(ACCOUNT_EQUITY) <= start * (1.0 - P.rk_daily_loss_limit));
  }

//+------------------------------------------------------------------+
//| 時刻（米東部時間で判定）                                             |
//+------------------------------------------------------------------+
datetime NthSunday(const int year, const int mon, const int nth)
  {
   MqlDateTime d;
   d.year = year;
   d.mon = mon;
   d.day = 1;
   d.hour = 0;
   d.min = 0;
   d.sec = 0;
   datetime first = StructToTime(d);
   MqlDateTime f;
   TimeToStruct(first, f);
   int first_sunday = 1 + (7 - f.day_of_week) % 7;
   d.day = first_sunday + (nth - 1) * 7;
   return(StructToTime(d));
  }

bool IsUsDstUtc(const datetime utc)
  {
   MqlDateTime d;
   TimeToStruct(utc, d);
   datetime start = NthSunday(d.year, 3, 2) + 7 * 3600;   // 2:00 EST = 7:00 UTC
   datetime end = NthSunday(d.year, 11, 1) + 6 * 3600;    // 2:00 EDT = 6:00 UTC
   return(utc >= start && utc < end);
  }

bool IsUsDstEt(const datetime et)
  {
   MqlDateTime d;
   TimeToStruct(et, d);
   datetime start = NthSunday(d.year, 3, 2) + 2 * 3600;
   datetime end = NthSunday(d.year, 11, 1) + 2 * 3600;
   return(et >= start && et < end);
  }

datetime UtcToET(const datetime utc)
  {
   return(utc - (IsUsDstUtc(utc) ? 4 : 5) * 3600);
  }

datetime ServerToET(const datetime s)
  {
   if(ea_server_tz == TZ_NY_CLOSE)
      return(s - 7 * 3600);
   return(UtcToET(s - (int)(ea_server_offset * 3600)));
  }

datetime ServerToUTC(const datetime s)
  {
   if(ea_server_tz == TZ_FIXED)
      return(s - (int)(ea_server_offset * 3600));
   datetime et = s - 7 * 3600;
   return(et + (IsUsDstEt(et) ? 4 : 5) * 3600);
  }

bool FridayCutoffPassed(const datetime et, const double cutoff_hour)
  {
   MqlDateTime d;
   TimeToStruct(et, d);
   double hour = d.hour + d.min / 60.0;
   if(d.day_of_week == 5)
      return(hour >= cutoff_hour);
   if(d.day_of_week == 6)
      return(true);
   if(d.day_of_week == 0)
      return(hour < 18.0);
   return(false);
  }

long TradingDayKey(const datetime et)
  {
   MqlDateTime d;
   TimeToStruct(et + 7 * 3600, d);  // 17:00 ET で翌取引日
   return(d.year * 10000 + d.mon * 100 + d.day);
  }

//+------------------------------------------------------------------+
//| イベント（API/EIA 定例 + CSV）                                      |
//+------------------------------------------------------------------+
bool HasTag(const string tag)
  {
   if(tag == _Symbol)
      return(true);
   for(int i = 0; i < ArraySize(g_tags); i++)
      if(g_tags[i] == tag)
         return(true);
   return(false);
  }

// t_et が「イベントの before 秒前 〜 after 秒後」に入るか
bool EventInWindow(const datetime t_et, const int before, const int after)
  {
   if(P.ft_oil_events && HasTag("oil"))
     {
      for(int d = -2; d <= 2; d++)
        {
         datetime day = (datetime)(((long)t_et / 86400 + d) * 86400);
         MqlDateTime s;
         TimeToStruct(day, s);
         datetime ev = 0;
         if(s.day_of_week == 2)
            ev = day + 16 * 3600 + 30 * 60;     // API 火 16:30 ET
         else
            if(s.day_of_week == 3)
               ev = day + 10 * 3600 + 30 * 60;  // EIA 水 10:30 ET
         if(ev > 0 && t_et >= ev - before && t_et <= ev + after)
            return(true);
        }
     }
   for(int i = 0; i < ArraySize(g_events); i++)
      if(HasTag(g_events[i].tag) && t_et >= g_events[i].et - before && t_et <= g_events[i].et + after)
         return(true);
   return(false);
  }

// (t_et, t_et + lead] にイベントがあるか
bool EventUpcoming(const datetime t_et, const int lead)
  {
   if(P.ft_oil_events && HasTag("oil"))
     {
      for(int d = -1; d <= 2; d++)
        {
         datetime day = (datetime)(((long)t_et / 86400 + d) * 86400);
         MqlDateTime s;
         TimeToStruct(day, s);
         datetime ev = 0;
         if(s.day_of_week == 2)
            ev = day + 16 * 3600 + 30 * 60;
         else
            if(s.day_of_week == 3)
               ev = day + 10 * 3600 + 30 * 60;
         if(ev > t_et && ev <= t_et + lead)
            return(true);
        }
     }
   for(int i = 0; i < ArraySize(g_events); i++)
      if(HasTag(g_events[i].tag) && g_events[i].et > t_et && g_events[i].et <= t_et + lead)
         return(true);
   return(false);
  }

void LoadEvents()
  {
   ArrayResize(g_events, 0);
   string tags = P.ft_event_tags;
   StringReplace(tags, " ", "");
   ArrayResize(g_tags, 0);
   if(tags != "")
      StringSplit(tags, ',', g_tags);
   if(ea_events_file == "" || !FileIsExist(ea_events_file, FILE_COMMON))
      return;
   int h = FileOpen(ea_events_file, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(h == INVALID_HANDLE)
      return;
   bool jst = false;
   bool header = true;
   while(!FileIsEnding(h))
     {
      string line = FileReadString(h);
      StringTrimLeft(line);
      StringTrimRight(line);
      if(line == "" || StringGetCharacter(line, 0) == '#')
         continue;
      string f[];
      if(StringSplit(line, ',', f) < 3)
         continue;
      if(header)
        {
         header = false;
         string c0 = f[0];
         StringTrimLeft(c0);
         StringTrimRight(c0);
         if(c0 == "time_jst" || c0 == "time_utc")
           {
            jst = c0 == "time_jst";
            continue;
           }
        }
      string ts = f[0];
      StringTrimLeft(ts);
      StringTrimRight(ts);
      StringReplace(ts, "-", ".");
      datetime t = StringToTime(ts);
      if(t == 0)
         continue;
      datetime utc = jst ? (datetime)(t - 9 * 3600) : t;
      int n = ArraySize(g_events);
      ArrayResize(g_events, n + 1);
      g_events[n].et = UtcToET(utc);
      g_events[n].name = f[1];
      string tag = f[2];
      StringTrimLeft(tag);
      StringTrimRight(tag);
      g_events[n].tag = tag;
     }
   FileClose(h);
   PrintFormat("loaded %d events", ArraySize(g_events));
  }

//+------------------------------------------------------------------+
//| パラメータ                                                         |
//+------------------------------------------------------------------+
void LoadDefaults()
  {
   P.strategy = (int)strategy;
   P.dc_entry_period = dc_entry_period;
   P.dc_exit_period = dc_exit_period;
   P.dc_trend_ema = dc_trend_ema;
   P.dc_buffer_atr = dc_buffer_atr;
   P.sq_bb_period = sq_bb_period;
   P.sq_bb_mult = sq_bb_mult;
   P.sq_kc_mult = sq_kc_mult;
   P.sq_box_period = sq_box_period;
   P.sq_min_squeeze_bars = sq_min_squeeze_bars;
   P.sq_stop_box_frac = sq_stop_box_frac;
   P.pb_fast_ema = pb_fast_ema;
   P.pb_slow_ema = pb_slow_ema;
   P.pb_slope_bars = pb_slope_bars;
   P.pb_adx_period = pb_adx_period;
   P.pb_adx_min = pb_adx_min;
   P.pb_touch_atr = pb_touch_atr;
   P.pb_setup_bars = pb_setup_bars;
   P.pb_swing_bars = pb_swing_bars;
   P.pb_stop_buffer_atr = pb_stop_buffer_atr;
   P.mr_period = mr_period;
   P.mr_z_entry = mr_z_entry;
   P.mr_exit_z = mr_exit_z;
   P.mr_rsi_period = mr_rsi_period;
   P.mr_rsi_low = mr_rsi_low;
   P.mr_rsi_high = mr_rsi_high;
   P.mr_adx_period = mr_adx_period;
   P.mr_adx_max = mr_adx_max;
   P.htf_minutes = htf_minutes;
   P.htf_ema = htf_ema;
   P.ex_atr_period = ex_atr_period;
   P.ex_init_stop_atr = ex_init_stop_atr;
   P.ex_min_stop_atr = ex_min_stop_atr;
   P.ex_max_stop_atr = ex_max_stop_atr;
   P.ex_breakeven_trigger_atr = ex_breakeven_trigger_atr;
   P.ex_breakeven_offset_atr = ex_breakeven_offset_atr;
   P.ex_trail_atr = ex_trail_atr;
   P.ex_min_update_atr = ex_min_update_atr;
   P.ex_partial_tp_r = ex_partial_tp_r;
   P.ex_partial_fraction = ex_partial_fraction;
   P.ex_time_stop_bars = ex_time_stop_bars;
   P.ex_time_stop_min_r = ex_time_stop_min_r;
   P.ex_max_hold_bars = ex_max_hold_bars;
   P.ex_flatten_before_weekend = ex_flatten_before_weekend;
   P.ex_flatten_before_events = ex_flatten_before_events;
   P.rk_risk_per_trade = rk_risk_per_trade;
   P.rk_max_total_risk = rk_max_total_risk;
   P.rk_cluster_id = rk_cluster_id;
   P.rk_cluster_max_risk = rk_cluster_max_risk;
   P.rk_max_positions_per_symbol = rk_max_positions_per_symbol;
   P.rk_daily_loss_limit = rk_daily_loss_limit;
   P.rk_max_drawdown_halt = rk_max_drawdown_halt;
   P.rk_max_margin_utilization = rk_max_margin_utilization;
   P.rk_max_leverage_symbol = rk_max_leverage_symbol;
   P.rk_max_leverage_total = rk_max_leverage_total;
   P.rk_min_lot_overshoot = rk_min_lot_overshoot;
   P.rk_max_lots = rk_max_lots;
   P.ft_oil_events = ft_oil_events;
   P.ft_event_tags = ft_event_tags;
   P.ft_event_block_before_min = ft_event_block_before_min;
   P.ft_event_block_after_min = ft_event_block_after_min;
   P.ft_no_entry_after_fri_et = ft_no_entry_after_fri_et;
   P.ft_weekend_flatten_fri_et = ft_weekend_flatten_fri_et;
   P.ft_max_spread_points = ft_max_spread_points;
  }

bool ToBool(string v)
  {
   StringToLower(v);
   return(v == "true" || v == "1" || v == "yes");
  }

bool SetParam(const string key, const string v)
  {
   double d = StringToDouble(v);
   int i = (int)StringToInteger(v);
   if(key == "strategy")
     {
      string s = v;
      StringToLower(s);
      if(s == "donchian") P.strategy = STRAT_DONCHIAN;
      else if(s == "squeeze") P.strategy = STRAT_SQUEEZE;
      else if(s == "pullback") P.strategy = STRAT_PULLBACK;
      else if(s == "reversion") P.strategy = STRAT_REVERSION;
      else P.strategy = i;
     }
   else if(key == "dc_entry_period") P.dc_entry_period = i;
   else if(key == "dc_exit_period") P.dc_exit_period = i;
   else if(key == "dc_trend_ema") P.dc_trend_ema = i;
   else if(key == "dc_buffer_atr") P.dc_buffer_atr = d;
   else if(key == "sq_bb_period") P.sq_bb_period = i;
   else if(key == "sq_bb_mult") P.sq_bb_mult = d;
   else if(key == "sq_kc_mult") P.sq_kc_mult = d;
   else if(key == "sq_box_period") P.sq_box_period = i;
   else if(key == "sq_min_squeeze_bars") P.sq_min_squeeze_bars = i;
   else if(key == "sq_stop_box_frac") P.sq_stop_box_frac = d;
   else if(key == "pb_fast_ema") P.pb_fast_ema = i;
   else if(key == "pb_slow_ema") P.pb_slow_ema = i;
   else if(key == "pb_slope_bars") P.pb_slope_bars = i;
   else if(key == "pb_adx_period") P.pb_adx_period = i;
   else if(key == "pb_adx_min") P.pb_adx_min = d;
   else if(key == "pb_touch_atr") P.pb_touch_atr = d;
   else if(key == "pb_setup_bars") P.pb_setup_bars = i;
   else if(key == "pb_swing_bars") P.pb_swing_bars = i;
   else if(key == "pb_stop_buffer_atr") P.pb_stop_buffer_atr = d;
   else if(key == "mr_period") P.mr_period = i;
   else if(key == "mr_z_entry") P.mr_z_entry = d;
   else if(key == "mr_exit_z") P.mr_exit_z = d;
   else if(key == "mr_rsi_period") P.mr_rsi_period = i;
   else if(key == "mr_rsi_low") P.mr_rsi_low = d;
   else if(key == "mr_rsi_high") P.mr_rsi_high = d;
   else if(key == "mr_adx_period") P.mr_adx_period = i;
   else if(key == "mr_adx_max") P.mr_adx_max = d;
   else if(key == "htf_minutes") P.htf_minutes = i;
   else if(key == "htf_ema") P.htf_ema = i;
   else if(key == "ex_atr_period") P.ex_atr_period = i;
   else if(key == "ex_init_stop_atr") P.ex_init_stop_atr = d;
   else if(key == "ex_min_stop_atr") P.ex_min_stop_atr = d;
   else if(key == "ex_max_stop_atr") P.ex_max_stop_atr = d;
   else if(key == "ex_breakeven_trigger_atr") P.ex_breakeven_trigger_atr = d;
   else if(key == "ex_breakeven_offset_atr") P.ex_breakeven_offset_atr = d;
   else if(key == "ex_trail_atr") P.ex_trail_atr = d;
   else if(key == "ex_min_update_atr") P.ex_min_update_atr = d;
   else if(key == "ex_partial_tp_r") P.ex_partial_tp_r = d;
   else if(key == "ex_partial_fraction") P.ex_partial_fraction = d;
   else if(key == "ex_time_stop_bars") P.ex_time_stop_bars = i;
   else if(key == "ex_time_stop_min_r") P.ex_time_stop_min_r = d;
   else if(key == "ex_max_hold_bars") P.ex_max_hold_bars = i;
   else if(key == "ex_flatten_before_weekend") P.ex_flatten_before_weekend = ToBool(v);
   else if(key == "ex_flatten_before_events") P.ex_flatten_before_events = ToBool(v);
   else if(key == "rk_risk_per_trade") P.rk_risk_per_trade = d;
   else if(key == "rk_max_total_risk") P.rk_max_total_risk = d;
   else if(key == "rk_cluster_id") P.rk_cluster_id = i;
   else if(key == "rk_cluster_max_risk") P.rk_cluster_max_risk = d;
   else if(key == "rk_max_positions_per_symbol") P.rk_max_positions_per_symbol = i;
   else if(key == "rk_daily_loss_limit") P.rk_daily_loss_limit = d;
   else if(key == "rk_max_drawdown_halt") P.rk_max_drawdown_halt = d;
   else if(key == "rk_max_margin_utilization") P.rk_max_margin_utilization = d;
   else if(key == "rk_max_leverage_symbol") P.rk_max_leverage_symbol = d;
   else if(key == "rk_max_leverage_total") P.rk_max_leverage_total = d;
   else if(key == "rk_min_lot_overshoot") P.rk_min_lot_overshoot = d;
   else if(key == "rk_max_lots") P.rk_max_lots = d;
   else if(key == "ft_oil_events") P.ft_oil_events = ToBool(v);
   else if(key == "ft_event_tags") P.ft_event_tags = v;
   else if(key == "ft_event_block_before_min") P.ft_event_block_before_min = i;
   else if(key == "ft_event_block_after_min") P.ft_event_block_after_min = i;
   else if(key == "ft_no_entry_after_fri_et") P.ft_no_entry_after_fri_et = d;
   else if(key == "ft_weekend_flatten_fri_et") P.ft_weekend_flatten_fri_et = d;
   else if(key == "ft_max_spread_points") P.ft_max_spread_points = i;
   else if(key == "ea_timeframe_minutes") return(true);  // 起動時のみ確認（.set で指定）
   else return(false);
   return(true);
  }

void LoadParamFile(const string name)
  {
   g_last_reload = TimeCurrent();
   int h = FileOpen(name, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(h == INVALID_HANDLE)
     {
      PrintFormat("param file not found: %s (Common\\Files)", name);
      return;
     }
   int n = 0;
   while(!FileIsEnding(h))
     {
      string line = FileReadString(h);
      StringTrimLeft(line);
      StringTrimRight(line);
      if(line == "" || StringGetCharacter(line, 0) == '#')
         continue;
      int pos = StringFind(line, "=");
      if(pos <= 0)
         continue;
      string key = StringSubstr(line, 0, pos);
      string val = StringSubstr(line, pos + 1);
      StringTrimRight(key);
      StringTrimLeft(val);
      if(SetParam(key, val))
         n++;
      else
         PrintFormat("unknown param: %s", key);
     }
   FileClose(h);
   PrintFormat("loaded %d params from %s (strategy=%d)", n, name, P.strategy);
   LoadEvents();
  }

//+------------------------------------------------------------------+
//| 補助                                                               |
//+------------------------------------------------------------------+
double GVGet(const string name, const double def)
  {
   if(!GlobalVariableCheck(name))
      return(def);
   return(GlobalVariableGet(name));
  }

string PosKey(const ulong ticket)
  {
   return("cfdbot.p." + IntegerToString((long)ticket) + ".");
  }

void CleanupPositionGVs()
  {
   for(int i = GlobalVariablesTotal() - 1; i >= 0; i--)
     {
      string name = GlobalVariableName(i);
      if(StringFind(name, "cfdbot.p.") != 0)
         continue;
      string rest = StringSubstr(name, 9);
      int dot = StringFind(rest, ".");
      if(dot <= 0)
         continue;
      ulong ticket = (ulong)StringToInteger(StringSubstr(rest, 0, dot));
      if(!PositionSelectByTicket(ticket))
         GlobalVariableDel(name);
     }
  }

double CurrentSpread()
  {
   return(SymbolInfoDouble(_Symbol, SYMBOL_ASK) - SymbolInfoDouble(_Symbol, SYMBOL_BID));
  }

double NormalizePrice(const double price)
  {
   double tick = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tick <= 0)
      tick = _Point;
   return(NormalizeDouble(MathRound(price / tick) * tick, _Digits));
  }

double NormalizeVolumeDown(const double v)
  {
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0)
      step = 0.01;
   double lots = MathFloor(v / step + 1e-9) * step;
   int digits = (int)MathMax(0, MathCeil(-MathLog10(step) - 1e-9));
   return(NormalizeDouble(lots, digits));
  }

void Notify(const string msg)
  {
   Print(msg);
   if(MQLInfoInteger(MQL_TESTER) || MQLInfoInteger(MQL_OPTIMIZATION))
      return;
   if(ea_push_notify)
      SendNotification(msg);
   if(ea_webhook_url != "")
     {
      string body = "{\"source\":\"cfdbot-ea\",\"symbol\":\"" + _Symbol + "\",\"magic\":" + IntegerToString(ea_magic) +
                    ",\"message\":\"" + msg + "\"}";
      char data[], result[];
      string res_headers;
      int len = StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
      if(len > 0)
         ArrayResize(data, len - 1);  // 末尾の NUL を除く
      int code = WebRequest("POST", ea_webhook_url, "Content-Type: application/json\r\n", 3000, data, result, res_headers);
      if(code < 200 || code >= 300)
         PrintFormat("webhook failed: %d (err=%d)", code, GetLastError());
     }
  }

void LogSignal(const int k, const CfdSignal &s)
  {
   string name = "cfdbot_signals_" + _Symbol + "_" + IntegerToString(ea_magic) + ".csv";
   bool exists = FileIsExist(name, FILE_COMMON);
   int h = FileOpen(name, FILE_READ | FILE_WRITE | FILE_CSV | FILE_ANSI | FILE_COMMON, ',');
   if(h == INVALID_HANDLE)
      return;
   if(!exists)
      FileWrite(h, "time_utc", "close", "atr", "entry", "stop_dist", "exit_long", "exit_short");
   FileSeek(h, 0, SEEK_END);
   FileWrite(h, TimeToString(ServerToUTC(g_t[k]), TIME_DATE | TIME_MINUTES), DoubleToString(g_c[k], _Digits),
             DoubleToString(g_atr[k], 6), s.entry, DoubleToString(s.stop_dist, 6), (int)s.exit_long, (int)s.exit_short);
   FileClose(h);
  }
//+------------------------------------------------------------------+
