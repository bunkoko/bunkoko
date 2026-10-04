//+------------------------------------------------------------------+
//| CfdExportBars.mq5                                                |
//| 学習用のバーを一括で CSV に書き出すスクリプト                       |
//|                                                                  |
//| 「表示 → 銘柄 → バー」で 1 本ずつ書き出すのと同じ形式               |
//| （<DATE> <TIME> <OPEN> ... <SPREAD>、時刻はサーバー時刻）を          |
//| 共通フォルダ Common\Files\<out_folder>\<銘柄>_<時間足>.csv に作る。  |
//| 銘柄仕様（symbol_specs.txt）と口座情報（account_info.txt）も書く。   |
//| Mac では python scripts/mt5_files.py fetch-data で data/ に取り込む。|
//|                                                                  |
//| 使い方: ナビゲータ → スクリプト → CfdExportBars を任意のチャートへ    |
//| ドラッグ → 入力を確認して OK。終わると「エキスパート」タブに結果。     |
//+------------------------------------------------------------------+
#property copyright "cfdbot"
#property version   "0.12"
#property description "Export bars of several symbols/timeframes to Common\\Files for cfdbot"
#property script_show_inputs

input string   symbols    = "XAGUSD,XAUUSD,XTIUSD,XBRUSD";        // 銘柄（MT5 の名前、カンマ区切り）
input string   timeframes = "M5,H1";                              // 時間足（カンマ区切り）
input string   fx_symbol  = "USDJPY";                             // 円換算用（H1 だけ書き出す。空=書き出さない）
input datetime from_date  = D'2020.12.01 00:00';                  // この日から
input string   out_folder = "cfdbot_data";                        // Common\Files の下のフォルダ
input int      wait_sec   = 300;                                  // 履歴のダウンロードを待つ最大秒数（1 組あたり）

ENUM_TIMEFRAMES ParseTimeframe(const string s)
  {
   if(s == "M1")  return(PERIOD_M1);
   if(s == "M5")  return(PERIOD_M5);
   if(s == "M10") return(PERIOD_M10);
   if(s == "M15") return(PERIOD_M15);
   if(s == "M30") return(PERIOD_M30);
   if(s == "H1")  return(PERIOD_H1);
   if(s == "H4")  return(PERIOD_H4);
   if(s == "D1")  return(PERIOD_D1);
   return(PERIOD_CURRENT);
  }

string Trimmed(string s)
  {
   StringTrimLeft(s);
   StringTrimRight(s);
   return(s);
  }

// サーバーにある履歴の最初の日時（MT5 はどの時間足も M1 の履歴から作るので、時間足によらない）。不明なら 0
datetime ServerFirstDate(const string sym)
  {
   long first = 0;
   for(int i = 0; i < 20 && !IsStopped(); i++)
     {
      if(SeriesInfoInteger(sym, PERIOD_M1, SERIES_SERVER_FIRSTDATE, first) && first > 0)
         return((datetime)first);
      Sleep(500);
     }
   return(0);
  }

// target（指定日とサーバーの最初の日の遅い方）まで遡れるまで、履歴のダウンロードを待ってコピーする。
// 最大バー数で頭打ちになったとき・30 秒増えないとき・wait_sec 秒たったときはそこまでで返す
int CopyAll(const string sym, const ENUM_TIMEFRAMES tf, const datetime target, MqlRates &rates[])
  {
   int last = -1, same = 0;
   for(int i = 0; i < wait_sec && !IsStopped(); i++)
     {
      ResetLastError();
      int n = CopyRates(sym, tf, from_date, TimeCurrent(), rates);
      if(n > 0 && rates[0].time <= target + 7 * 86400)
         return(n);
      if(n > 0 && n >= TerminalInfoInteger(TERMINAL_MAXBARS))
         return(n);
      same = (n == last) ? same + 1 : 0;
      if(n > 0 && same >= 30)
         return(n);
      last = n;
      Sleep(1000);
     }
   return(last);
  }

bool ExportOne(const string sym, const string tf_name, bool &warned)
  {
   ENUM_TIMEFRAMES tf = ParseTimeframe(tf_name);
   if(tf == PERIOD_CURRENT)
     {
      PrintFormat("時間足 '%s' は使えない（M1,M5,M10,M15,M30,H1,H4,D1）", tf_name);
      return(false);
     }
   datetime server_first = ServerFirstDate(sym);
   datetime target = server_first > from_date ? server_first : from_date;
   MqlRates rates[];
   ArraySetAsSeries(rates, false);
   int n = CopyAll(sym, tf, target, rates);
   if(n <= 0)
     {
      PrintFormat("%s %s: バーを取得できない（error %d）", sym, tf_name, GetLastError());
      return(false);
     }
   string path = out_folder + "\\" + sym + "_" + tf_name + ".csv";
   int h = FileOpen(path, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(h == INVALID_HANDLE)
     {
      PrintFormat("%s: 書き込めない（error %d）", path, GetLastError());
      return(false);
     }
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   FileWriteString(h, "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\r\n");
   for(int i = 0; i < n; i++)
     {
      FileWriteString(h, StringFormat("%s\t%s\t%s\t%s\t%s\t%s\t%I64d\t%I64d\t%d\r\n",
                                      TimeToString(rates[i].time, TIME_DATE),
                                      TimeToString(rates[i].time, TIME_SECONDS),
                                      DoubleToString(rates[i].open, digits),
                                      DoubleToString(rates[i].high, digits),
                                      DoubleToString(rates[i].low, digits),
                                      DoubleToString(rates[i].close, digits),
                                      rates[i].tick_volume, rates[i].real_volume, rates[i].spread));
     }
   FileClose(h);
   string note = "";
   if(n >= TerminalInfoInteger(TERMINAL_MAXBARS))
      note = "⚠ 最大バー数で頭打ち。ツール→オプション→チャート→最大バー数を Unlimited にし、MT5 を再起動して再実行";
   else
      if(rates[0].time > target + 7 * 86400)
         note = "⚠ 履歴のダウンロードが終わっていない。もう一度実行する";
      else
         if(server_first > from_date + 7 * 86400)
            note = "（サーバーの履歴がこの日から。これより前は無い）";
   if(StringFind(note, "⚠") == 0)
      warned = true;
   PrintFormat("%s %s: %d 本 %s 〜 %s（サーバーの履歴: %s〜）→ Common\\Files\\%s %s", sym, tf_name, n,
               TimeToString(rates[0].time, TIME_DATE), TimeToString(rates[n - 1].time, TIME_DATE),
               server_first > 0 ? TimeToString(server_first, TIME_DATE) : "不明", path, note);
   return(true);
  }

string Dbl(const double v)
  {
   return(StringFormat("%.10g", v));
  }

// 銘柄仕様を 1 銘柄 1 行（タブ区切り）で書く。Python 側（cfdbot/mt5specs.py）が銘柄仕様に換算する
bool WriteSpecs(string &names[])
  {
   string path = out_folder + "\\symbol_specs.txt";
   int h = FileOpen(path, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(h == INVALID_HANDLE)
     {
      PrintFormat("%s: 書き込めない（error %d）", path, GetLastError());
      return(false);
     }
   FileWriteString(h, "symbol\tdigits\tpoint\ttick_size\ttick_value\tcontract_size\tvolume_min\tvolume_step" +
                   "\tvolume_max\tvolume_limit\tstops_level\tfreeze_level\tswap_mode\tswap_long\tswap_short" +
                   "\tswap_3days\tcurrency_base\tcurrency_profit\tcurrency_margin\tcalc_mode\ttrade_mode" +
                   "\tfilling_mode\tbid\task\tspread_points\tmargin_1lot\texpiration\r\n");
   for(int i = 0; i < ArraySize(names); i++)
     {
      string sym = names[i];
      double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
      double margin = 0;
      if(ask <= 0 || !OrderCalcMargin(ORDER_TYPE_BUY, sym, 1.0, ask, margin))
         margin = 0;
      string row = sym;
      row += "\t" + IntegerToString(SymbolInfoInteger(sym, SYMBOL_DIGITS));
      row += "\t" + Dbl(SymbolInfoDouble(sym, SYMBOL_POINT));
      row += "\t" + Dbl(SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE));
      row += "\t" + Dbl(SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE));
      row += "\t" + Dbl(SymbolInfoDouble(sym, SYMBOL_TRADE_CONTRACT_SIZE));
      row += "\t" + Dbl(SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN));
      row += "\t" + Dbl(SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP));
      row += "\t" + Dbl(SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX));
      row += "\t" + Dbl(SymbolInfoDouble(sym, SYMBOL_VOLUME_LIMIT));
      row += "\t" + IntegerToString(SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL));
      row += "\t" + IntegerToString(SymbolInfoInteger(sym, SYMBOL_TRADE_FREEZE_LEVEL));
      row += "\t" + EnumToString((ENUM_SYMBOL_SWAP_MODE)SymbolInfoInteger(sym, SYMBOL_SWAP_MODE));
      row += "\t" + Dbl(SymbolInfoDouble(sym, SYMBOL_SWAP_LONG));
      row += "\t" + Dbl(SymbolInfoDouble(sym, SYMBOL_SWAP_SHORT));
      row += "\t" + EnumToString((ENUM_DAY_OF_WEEK)SymbolInfoInteger(sym, SYMBOL_SWAP_ROLLOVER3DAYS));
      row += "\t" + SymbolInfoString(sym, SYMBOL_CURRENCY_BASE);
      row += "\t" + SymbolInfoString(sym, SYMBOL_CURRENCY_PROFIT);
      row += "\t" + SymbolInfoString(sym, SYMBOL_CURRENCY_MARGIN);
      row += "\t" + EnumToString((ENUM_SYMBOL_CALC_MODE)SymbolInfoInteger(sym, SYMBOL_TRADE_CALC_MODE));
      row += "\t" + EnumToString((ENUM_SYMBOL_TRADE_MODE)SymbolInfoInteger(sym, SYMBOL_TRADE_MODE));
      row += "\t" + IntegerToString(SymbolInfoInteger(sym, SYMBOL_FILLING_MODE));
      row += "\t" + Dbl(SymbolInfoDouble(sym, SYMBOL_BID));
      row += "\t" + Dbl(ask);
      row += "\t" + IntegerToString(SymbolInfoInteger(sym, SYMBOL_SPREAD));
      row += "\t" + Dbl(margin);
      row += "\t" + IntegerToString(SymbolInfoInteger(sym, SYMBOL_EXPIRATION_TIME));
      FileWriteString(h, row + "\r\n");
     }
   FileClose(h);
   PrintFormat("銘柄仕様 %d 銘柄 → Common\\Files\\%s", ArraySize(names), path);
   return(true);
  }

// 口座情報（会社・サーバー・口座通貨・レバレッジ・ネッティング/ヘッジング・デモ/本口座）
bool WriteAccount()
  {
   string path = out_folder + "\\account_info.txt";
   int h = FileOpen(path, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(h == INVALID_HANDLE)
      return(false);
   FileWriteString(h, "company=" + AccountInfoString(ACCOUNT_COMPANY) + "\r\n");
   FileWriteString(h, "server=" + AccountInfoString(ACCOUNT_SERVER) + "\r\n");
   FileWriteString(h, "currency=" + AccountInfoString(ACCOUNT_CURRENCY) + "\r\n");
   FileWriteString(h, "leverage=" + IntegerToString(AccountInfoInteger(ACCOUNT_LEVERAGE)) + "\r\n");
   FileWriteString(h, "margin_mode=" + EnumToString((ENUM_ACCOUNT_MARGIN_MODE)AccountInfoInteger(ACCOUNT_MARGIN_MODE)) + "\r\n");
   FileWriteString(h, "trade_mode=" + EnumToString((ENUM_ACCOUNT_TRADE_MODE)AccountInfoInteger(ACCOUNT_TRADE_MODE)) + "\r\n");
   FileWriteString(h, "stopout_mode=" + EnumToString((ENUM_ACCOUNT_STOPOUT_MODE)AccountInfoInteger(ACCOUNT_MARGIN_SO_MODE)) + "\r\n");
   FileWriteString(h, "margin_call=" + Dbl(AccountInfoDouble(ACCOUNT_MARGIN_SO_CALL)) + "\r\n");
   FileWriteString(h, "stopout=" + Dbl(AccountInfoDouble(ACCOUNT_MARGIN_SO_SO)) + "\r\n");
   FileWriteString(h, "build=" + IntegerToString(TerminalInfoInteger(TERMINAL_BUILD)) + "\r\n");
   FileWriteString(h, "max_bars=" + IntegerToString(TerminalInfoInteger(TERMINAL_MAXBARS)) + "\r\n");
   FileWriteString(h, "exported=" + TimeToString(TimeCurrent(), TIME_DATE | TIME_MINUTES) + "\r\n");
   FileClose(h);
   return(true);
  }

void OnStart()
  {
   string syms[], tfs[];
   StringSplit(symbols, ',', syms);
   StringSplit(timeframes, ',', tfs);
   FolderCreate(out_folder, FILE_COMMON);
   int ok = 0, total = 0, warn = 0;
   string found[];
   for(int i = 0; i < ArraySize(syms) && !IsStopped(); i++)
     {
      string sym = Trimmed(syms[i]);
      if(sym == "")
         continue;
      if(!SymbolSelect(sym, true))
        {
         PrintFormat("%s: 銘柄が見つからない（気配値表示の「すべて表示」で名前を確認）", sym);
         total += ArraySize(tfs);
         continue;
        }
      ArrayResize(found, ArraySize(found) + 1);
      found[ArraySize(found) - 1] = sym;
      for(int j = 0; j < ArraySize(tfs) && !IsStopped(); j++)
        {
         string tf = Trimmed(tfs[j]);
         if(tf == "")
            continue;
         total++;
         bool w = false;
         if(ExportOne(sym, tf, w))
            ok++;
         if(w)
            warn++;
        }
     }
   string fx = Trimmed(fx_symbol);
   if(fx != "" && !IsStopped())
     {
      total++;
      if(!SymbolSelect(fx, true))
         PrintFormat("%s: 銘柄が見つからない（気配値表示の「すべて表示」で名前を確認）", fx);
      else
        {
         ArrayResize(found, ArraySize(found) + 1);
         found[ArraySize(found) - 1] = fx;
         bool w = false;
         if(ExportOne(fx, "H1", w))
            ok++;
         if(w)
            warn++;
        }
     }
   WriteSpecs(found);
   WriteAccount();
   string msg = StringFormat("CfdExportBars: %d / %d ファイルを書き出した（Common\\Files\\%s）%s", ok, total, out_folder,
                             warn > 0 ? StringFormat("。⚠ %d 件は期間が足りない（エキスパートタブを見る）", warn) : "");
   Print(msg);
   Alert(msg);
  }
//+------------------------------------------------------------------+
