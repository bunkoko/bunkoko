//+------------------------------------------------------------------+
//| CfdExportBars.mq5                                                |
//| 学習用のバーを一括で CSV に書き出すスクリプト                       |
//|                                                                  |
//| 「表示 → 銘柄 → バー」で 1 本ずつ書き出すのと同じ形式               |
//| （<DATE> <TIME> <OPEN> ... <SPREAD>、時刻はサーバー時刻）を          |
//| 共通フォルダ Common\Files\<out_folder>\<銘柄>_<時間足>.csv に作る。  |
//| Mac では python scripts/mt5_files.py fetch-data で data/ に取り込む。|
//|                                                                  |
//| 使い方: ナビゲータ → スクリプト → CfdExportBars を任意のチャートへ    |
//| ドラッグ → 入力を確認して OK。終わると「エキスパート」タブに結果。     |
//+------------------------------------------------------------------+
#property copyright "cfdbot"
#property version   "0.10"
#property description "Export bars of several symbols/timeframes to Common\\Files for cfdbot"
#property script_show_inputs

input string   symbols    = "XAGUSD,XAUUSD,XTIUSD,XBRUSD";        // 銘柄（MT5 の名前、カンマ区切り）
input string   timeframes = "M5,H1";                              // 時間足（カンマ区切り）
input string   fx_symbol  = "USDJPY";                             // 円換算用（H1 だけ書き出す。空=書き出さない）
input datetime from_date  = D'2020.12.01 00:00';                  // この日から
input string   out_folder = "cfdbot_data";                        // Common\Files の下のフォルダ
input int      wait_sec   = 120;                                  // 履歴のダウンロードを待つ最大秒数（1 組あたり）

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

// 履歴のダウンロードが進まなくなるまで待ってから全部コピーする
int CopyAll(const string sym, const ENUM_TIMEFRAMES tf, MqlRates &rates[])
  {
   int last = -1, same = 0;
   for(int i = 0; i < wait_sec && !IsStopped(); i++)
     {
      ResetLastError();
      int n = CopyRates(sym, tf, from_date, TimeCurrent(), rates);
      bool synced = SeriesInfoInteger(sym, tf, SERIES_SYNCHRONIZED) != 0;
      if(n > 0 && (rates[0].time <= from_date + 7 * 86400 || (synced && n == last && ++same >= 3)))
         return(n);
      if(n != last)
         same = 0;
      last = n;
      Sleep(1000);
     }
   return(last);
  }

bool ExportOne(const string sym, const string tf_name)
  {
   ENUM_TIMEFRAMES tf = ParseTimeframe(tf_name);
   if(tf == PERIOD_CURRENT)
     {
      PrintFormat("時間足 '%s' は使えない（M1,M5,M10,M15,M30,H1,H4,D1）", tf_name);
      return(false);
     }
   MqlRates rates[];
   ArraySetAsSeries(rates, false);
   int n = CopyAll(sym, tf, rates);
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
   string note = rates[0].time > from_date + 7 * 86400 ?
                 "（指定日より新しい所から。ツール→オプション→チャート→最大バー数を Unlimited にして再実行するか、サーバーの履歴がそこまで）" : "";
   PrintFormat("%s %s: %d 本 %s 〜 %s → Common\\Files\\%s %s", sym, tf_name, n,
               TimeToString(rates[0].time, TIME_DATE), TimeToString(rates[n - 1].time, TIME_DATE), path, note);
   return(true);
  }

void OnStart()
  {
   string syms[], tfs[];
   StringSplit(symbols, ',', syms);
   StringSplit(timeframes, ',', tfs);
   FolderCreate(out_folder, FILE_COMMON);
   int ok = 0, total = 0;
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
      for(int j = 0; j < ArraySize(tfs) && !IsStopped(); j++)
        {
         string tf = Trimmed(tfs[j]);
         if(tf == "")
            continue;
         total++;
         if(ExportOne(sym, tf))
            ok++;
        }
     }
   string fx = Trimmed(fx_symbol);
   if(fx != "" && !IsStopped())
     {
      total++;
      if(!SymbolSelect(fx, true))
         PrintFormat("%s: 銘柄が見つからない（気配値表示の「すべて表示」で名前を確認）", fx);
      else
         if(ExportOne(fx, "H1"))
            ok++;
     }
   string msg = StringFormat("CfdExportBars: %d / %d ファイルを書き出した（Common\\Files\\%s）", ok, total, out_folder);
   Print(msg);
   Alert(msg);
  }
//+------------------------------------------------------------------+
