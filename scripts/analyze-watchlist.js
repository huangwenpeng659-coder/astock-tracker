#!/usr/bin/env node

const fs = require('fs');
const path = require('path');
const readline = require('readline');

const watchlistPath = path.join(__dirname, '../watchlist.json');

const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout
});

const question = (prompt) => new Promise(resolve => rl.question(prompt, resolve));

async function addStock() {
  console.log('\n📝 添加自选股\n');

  const code = await question('股票代码（如301357）: ');
  const name = await question('股票名称（如北方长龙）: ');
  const category = await question('分类（如军工、医药等）: ');
  const costStr = await question('买入成本（元）: ');
  const quantityStr = await question('持仓数量（股）: ');

  const cost = parseFloat(costStr);
  const quantity = parseInt(quantityStr);

  if (!code || !name || isNaN(cost) || isNaN(quantity)) {
    console.log('❌ 输入有误，请重试');
    return null;
  }

  return {
    code,
    name,
    cost,
    quantity,
    category,
    addedDate: new Date().toISOString().split('T')[0],
    currentPrice: cost,
    notes: ''
  };
}

function loadWatchlist() {
  if (!fs.existsSync(watchlistPath)) {
    return {
      stocks: [],
      metadata: {
        lastUpdated: new Date().toISOString().split('T')[0],
        analysisFramework: 'swing-trading-discipline'
      }
    };
  }
  return JSON.parse(fs.readFileSync(watchlistPath, 'utf8'));
}

function saveWatchlist(watchlist) {
  watchlist.metadata.lastUpdated = new Date().toISOString().split('T')[0];
  fs.writeFileSync(watchlistPath, JSON.stringify(watchlist, null, 2), 'utf8');
}

function generateAnalysis(stock) {
  const { code, name, cost, quantity, category, currentPrice } = stock;
  const totalCost = cost * quantity;
  const marketValue = (currentPrice || cost) * quantity;
  const floatProfit = marketValue - totalCost;
  const floatProfitRate = quantity > 0 ? ((floatProfit / totalCost) * 100).toFixed(2) : '0.00';

  return `# ${name}（${code}）- 波段交易分析

**分类**: ${category} | **成本**: ${cost}元 | **持仓**: ${quantity}股 | **总成本**: ¥${totalCost.toFixed(2)} | **当前价值**: ¥${marketValue.toFixed(2)} | **浮动盈亏**: ¥${floatProfit.toFixed(2)} (${floatProfitRate}%) | **生成时间**: ${new Date().toISOString().split('T')[0]}

---

## 📊 仓位信息速览

| 指标 | 数值 |
|------|------|
| 买入成本 | ${cost}元/股 |
| 持仓数量 | ${quantity}股 |
| 总投入 | ¥${totalCost.toFixed(2)} |
| 当前价格 | ${currentPrice}元/股 |
| 当前市值 | ¥${marketValue.toFixed(2)} |
| 浮动盈亏 | ¥${floatProfit.toFixed(2)} |
| 浮动收益率 | ${floatProfitRate}% |

---

## 待输入数据

为了完成分析，需要以下信息（请从看盘软件查询）：

| 指标 | 数值 | 说明 |
|------|------|------|
| 当前价格 | ${currentPrice}元 | 最新收盘价或实时价 |
| 日均换手率 | | 近20日平均 |
| 日均成交额 | | 近20日平均（万元） |
| 日均振幅 | | 近20日平均 |
| 52周高点 | | 上行阻力 |
| 52周低点 | | 下行支撑 |
| 近期高点 | | 最近1个月 |
| 近期低点 | | 最近1个月 |
| 5日均线方向 | | 向上/向下/横盘 |
| 20日均线方向 | | 向上/向下/横盘 |
| 成交量趋势 | | 放大/萎缩/均匀 |

## 择时检查表（待填）

| 维度 | 观察 | 信号 |
|------|------|------|
| 趋势 | | ⭕偏多 / 🔴偏空 / ⚪中性 |
| 成交量 | | ⭕活跃 / 🔴低迷 / ⚪均匀 |
| K线 | | ⭕规整 / 🔴混乱 / ⚪待观察 |
| 均线 | | ⭕多头 / 🔴空头 / ⚪盘整 |

## 筛选评分（待填）

按照 swing-trading-discipline 框架，逐项打分（符合=2分，一般=1分，不符合=0分）：

| # | 维度 | 合格标准 | 得分 |
|---|------|--------|------|
| 1 | 流动性 | 日换手率3%-10%，成交额≥1亿 | __ |
| 2 | 日内振幅 | 日均振幅3%-6% | __ |
| 3 | 趋势 | 区间震荡或缓慢上行 | __ |
| 4 | 区间清晰 | 有明确支撑位和压力位 | __ |
| 5 | 成交量 | 相对均匀 | __ |
| 6 | 基本面 | 近期无重大利空 | __ |
| 7 | 股东行为 | 无大额减持、解禁临近 | __ |
| 8 | 风险 | 不处于连板、一字板或停牌中 | __ |
| | **合计** | | **__ / 16** |

**评分解读**：
- 13-16分：比较适合做T
- 9-12分：谨慎做T，仓位减半
- 8分以下：不做T

## 做T规则（如果评分≥9分）

### 反T（先卖后买）
- **触发**: 冲高到压力位，量能跟不上
- **卖出价**: __元
- **接回价**: __元（约卖出价-3%到-4%）
- **放弃条件**: 继续放量上涨，站上新高

### 正T（先买后卖）
- **触发**: 回落到支撑位，缩量不创新低
- **买入价**: __元
- **卖出价**: __元（约买入价+3%）
- **放弃条件**: 跌破支撑位并放量

### 一票否决（出现任意一条，当天不做）
- [ ] 当天有重大公告或板块利空
- [ ] 开盘跳空涨停
- [ ] 心态烦躁或想翻本

## 风险提示

1. **单边行情风险**: 脉冲日涨跌常见 10%+，反T容易卖飞
2. **T+1 限制**: 当天买入不能当天卖
3. **趋势优先**: 区间被跌破应停止做T
4. **成本检查**: 预期价差需大于往返成本 3 倍以上

---

**框架性分析，不构成投资建议。**
`;
}

async function main() {
  const watchlist = loadWatchlist();

  console.log('\n📊 自选股分析系统\n');
  console.log('1. 添加新股票');
  console.log('2. 生成分析报告');
  console.log('3. 查看已有股票');

  const choice = await question('\n请选择操作 (1-3): ');

  if (choice === '1') {
    const stock = await addStock();
    if (stock) {
      watchlist.stocks.push(stock);
      saveWatchlist(watchlist);
      console.log(`\n✅ 已添加 ${stock.name}(${stock.code})`);
    }
  } else if (choice === '2') {
    if (watchlist.stocks.length === 0) {
      console.log('\n❌ 还没有添加任何股票');
    } else {
      console.log(`\n📋 共 ${watchlist.stocks.length} 只股票，正在生成分析报告...\n`);

      if (!fs.existsSync(path.join(__dirname, '../analysis'))) {
        fs.mkdirSync(path.join(__dirname, '../analysis'), { recursive: true });
      }

      watchlist.stocks.forEach((stock) => {
        const analysis = generateAnalysis(stock);
        const filename = `${stock.code}-${stock.name}-analysis.md`;
        const outputPath = path.join(__dirname, '../analysis', filename);
        fs.writeFileSync(outputPath, analysis, 'utf8');
        console.log(`✅ ${stock.name}(${stock.code}) → analysis/${filename}`);
      });

      console.log('\n💡 后续步骤：');
      console.log('1. 打开 analysis/ 文件夹中的分析文件');
      console.log('2. 从看盘软件查询各项数据，填入表格');
      console.log('3. 按评分标准完成筛选打分');
      console.log('4. 如评分≥9分，填写做T规则的具体价位\n');
    }
  } else if (choice === '3') {
    if (watchlist.stocks.length === 0) {
      console.log('\n❌ 还没有添加任何股票\n');
    } else {
      console.log(`\n📋 已有自选股（共 ${watchlist.stocks.length} 只）:\n`);
      watchlist.stocks.forEach((stock, idx) => {
        const totalCost = stock.cost * stock.quantity;
        console.log(`${idx + 1}. ${stock.name}(${stock.code})`);
        console.log(`   成本: ${stock.cost}元 | 持仓: ${stock.quantity}股 | 总投: ¥${totalCost.toFixed(2)}`);
        console.log(`   分类: ${stock.category}`);
        console.log('');
      });
    }
  }

  rl.close();
}

main().catch(console.error);
