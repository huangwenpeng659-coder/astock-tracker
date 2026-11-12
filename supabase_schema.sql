-- ============================================================
-- A股埋伏追踪终端 · Supabase 表结构（可重复执行）
-- 在 Supabase → SQL Editor 里整段执行一次即可
-- ============================================================

-- ---------- 股票基本信息 ----------
create table if not exists stocks (
  code text primary key,
  name text,
  is_st boolean default false,
  is_delisted boolean default false
);

-- ---------- 日K线 ----------
create table if not exists daily_kline (
  code text not null,
  trade_date date not null,
  open numeric, high numeric, low numeric, close numeric,
  volume bigint, amount bigint, turnover_rate numeric, change_pct numeric,
  primary key (code, trade_date)
);

-- ---------- 选股候选 ----------
create table if not exists candidates (
  code text not null,
  trade_date date not null,
  name text,
  score numeric,
  rank integer,
  last_price numeric,
  launch_point numeric,
  stop_loss numeric,
  target_1 numeric, target_2 numeric, target_3 numeric,
  signals jsonb,
  primary key (code, trade_date)
);

-- ---------- 候选追踪（复盘用） ----------
create table if not exists candidate_tracking (
  code text not null,
  select_date date not null,
  name text,
  select_price numeric,
  select_score numeric,
  current_price numeric,
  days_since integer default 0,
  pct_change numeric default 0,
  max_gain numeric default 0,
  max_loss numeric default 0,
  pct_5d numeric, pct_10d numeric, pct_20d numeric,
  hit_target integer default 0,
  hit_stop boolean default false,
  status text default '追踪中',
  primary key (code, select_date)
);

-- ---------- 当日信号（涨停 / 三日放量上涨） ----------
create table if not exists daily_signals (
  code text not null,
  trade_date date not null,
  signal_type text not null,
  name text,
  price numeric,
  details jsonb,
  primary key (code, trade_date, signal_type)
);

-- ---------- 自选股 ----------
create table if not exists watchlist (
  code text primary key,
  name text,
  created_at timestamptz default now()
);

-- ---------- 个股评分 ----------
create table if not exists stock_scores (
  code text primary key,
  name text,
  trade_date date,
  score numeric,
  grade text,
  signals jsonb,
  dims jsonb,
  summary text
);

-- ---------- 配置（含选股阈值 + last_run 运行状态） ----------
create table if not exists config (
  key text primary key,
  value jsonb
);

-- ---------- 日内做T策略 ----------
create table if not exists t_strategy (
  code text not null,
  trade_date date not null,
  name text,
  vwap numeric,
  signals jsonb,
  industry text,
  leaders jsonb,
  leader_correlation text,
  advice text,
  confidence text,
  min_kline jsonb,
  period text default '30',      -- 分钟周期（30=半小时级别）
  plan jsonb,                   -- 标准化做T计划：触发价/止损/目标/仓位
  buy_score numeric,
  sell_score numeric,
  concepts jsonb,               -- 所属概念
  primary key (code, trade_date)
);

-- ---------- 个股概念标签 ----------
create table if not exists stock_concepts (
  code text primary key,
  name text,
  concepts jsonb
);

-- ---------- 持仓追踪（可选） ----------
create table if not exists tracking (
  code text primary key,
  name text,
  buy_price numeric,
  shares numeric,
  buy_date date,
  status text default '持仓中'
);

-- ============================================================
-- 新增列（老库升级用，若表已存在则补齐）
-- ============================================================
alter table candidates add column if not exists name text;
alter table stock_scores add column if not exists grade text;
alter table stock_scores add column if not exists signals jsonb;
alter table stock_scores add column if not exists dims jsonb;
alter table stock_scores add column if not exists summary text;
alter table candidate_tracking add column if not exists name text;
alter table candidate_tracking add column if not exists pct_5d numeric;
alter table candidate_tracking add column if not exists pct_10d numeric;
alter table candidate_tracking add column if not exists pct_20d numeric;
alter table t_strategy add column if not exists period text;
alter table t_strategy add column if not exists plan jsonb;
alter table t_strategy add column if not exists buy_score numeric;
alter table t_strategy add column if not exists sell_score numeric;
alter table t_strategy add column if not exists concepts jsonb;

-- ============================================================
-- RLS + anon 读写策略（前端用 publishable key 直接读写）
-- ============================================================
alter table stocks enable row level security;
alter table daily_kline enable row level security;
alter table candidates enable row level security;
alter table candidate_tracking enable row level security;
alter table daily_signals enable row level security;
alter table watchlist enable row level security;
alter table stock_scores enable row level security;
alter table config enable row level security;
alter table t_strategy enable row level security;
alter table stock_concepts enable row level security;
alter table tracking enable row level security;

drop policy if exists "allow all" on stocks for all to anon using (true) with check (true);
drop policy if exists "allow all" on daily_kline for all to anon using (true) with check (true);
drop policy if exists "allow all" on candidates for all to anon using (true) with check (true);
drop policy if exists "allow all" on candidate_tracking for all to anon using (true) with check (true);
drop policy if exists "allow all" on daily_signals for all to anon using (true) with check (true);
drop policy if exists "allow all" on watchlist for all to anon using (true) with check (true);
drop policy if exists "allow all" on stock_scores for all to anon using (true) with check (true);
drop policy if exists "allow all" on config for all to anon using (true) with check (true);
drop policy if exists "allow all" on t_strategy for all to anon using (true) with check (true);
drop policy if exists "allow all" on stock_concepts for all to anon using (true) with check (true);
drop policy if exists "allow all" on tracking for all to anon using (true) with check (true);

create policy "allow all" on stocks for all to anon using (true) with check (true);
create policy "allow all" on daily_kline for all to anon using (true) with check (true);
create policy "allow all" on candidates for all to anon using (true) with check (true);
create policy "allow all" on candidate_tracking for all to anon using (true) with check (true);
create policy "allow all" on daily_signals for all to anon using (true) with check (true);
create policy "allow all" on watchlist for all to anon using (true) with check (true);
create policy "allow all" on stock_scores for all to anon using (true) with check (true);
create policy "allow all" on config for all to anon using (true) with check (true);
create policy "allow all" on t_strategy for all to anon using (true) with check (true);
create policy "allow all" on stock_concepts for all to anon using (true) with check (true);
create policy "allow all" on tracking for all to anon using (true) with check (true);
