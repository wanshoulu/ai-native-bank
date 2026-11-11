'use strict';

/**
 * 推荐服务：模拟定时 Agent 任务的千人千面推荐引擎。
 * 每次调用时聚合用户的多域数据（账户、信用卡、理财、贷款、缴费），
 * 按确定性规则生成个性化推荐卡片列表。
 *
 * 设计原则：
 * - 不修改任何已有服务，只读取数据
 * - 推荐项结构统一，前端按 type 分发渲染
 * - 每条推荐携带 detail（定时任务已完成的推理结果）与 actions（后续动作），
 *   用户点击卡片直接查看分析结论，再通过 CTA 触发对应 skill / 功能页
 * - generatedAt 模拟"每日刷新"时间戳
 */

const { moneyValue } = require('./bank-service');

function createRecommendationService(options) {
  const opts = options || {};
  const nextActionService = opts.nextActionService;
  const repository = opts.repository;
  const bankService = opts.bankService;
  const wealthService = opts.wealthService;
  const creditCardService = opts.creditCardService;
  const loanService = opts.loanService;
  const experienceRegistry = opts.experienceRegistry;
  const artifactProvider = opts.artifactProvider;
  const now = typeof opts.now === 'function' ? opts.now : () => new Date().toISOString();

  if (nextActionService && typeof nextActionService.list !== 'function'
    && typeof nextActionService.listCompatibility !== 'function') {
    throw new Error('recommendation service nextActionService must expose list or listCompatibility');
  }
  if (!nextActionService && (!repository || !bankService)) {
    throw new Error('recommendation service requires nextActionService or legacy dependencies');
  }

  /**
   * 生成每日推荐：聚合多域数据，按优先级输出推荐卡片。
   */
  async function getLegacyDailyRecommendations(customerId) {
    await bankService.getCustomer(customerId);

    // 并行拉取各域数据
    const [accounts, cards, holdings, salaryInsights, wealthRecos, loans] = await Promise.all([
      bankService.listAccounts(customerId).catch(() => []),
      creditCardService ? creditCardService.listCards(customerId).catch(() => []) : [],
      wealthService ? wealthService.listHoldings(customerId).catch(() => []) : [],
      wealthService ? wealthService.getSalaryInsights(customerId).catch(() => null) : null,
      wealthService ? wealthService.getRecommendations(customerId).catch(() => null) : null,
      loanService ? loanService.listLoans(customerId).catch(() => null) : null,
    ]);

    // 计算总可用余额
    let liquidMinor = 0;
    for (const account of accounts) {
      const balance = await repository.getBalance(account.id).catch(() => null);
      if (balance) liquidMinor += balance.availableMinor;
    }

    const recommendations = [];

    /* ---- 1. 信用卡账单到期提醒（高优先级） ---- */
    if (cards && cards.length > 0) {
      for (const card of cards) {
        if (card.status !== 'ACTIVE') continue;
        const bill = creditCardService ? await creditCardService.getBill(customerId, card.id).catch(() => null) : null;
        const dueMinor = bill ? bill.amountDueMinor : (card.totalLimitMinor - card.availableLimitMinor);
        if (dueMinor > 0) {
          const daysToDue = bill && bill.dueDate ? Math.ceil((new Date(bill.dueDate) - new Date(now())) / 86400000) : 15;
          const cardName = card.productName || '龙卡';
          recommendations.push({
            id: 'rec-card-due-' + card.id,
            type: 'bill-alert',
            priority: daysToDue <= 3 ? 'critical' : 'high',
            title: '信用卡账单待还',
            description: cardName + ' · 尾号 ' + (card.maskedNo || '').slice(-4) + ' · 待还 ¥' + formatMinor(dueMinor),
            tag: daysToDue <= 3 ? daysToDue + ' 天后到期' : '到期日 ' + (bill && bill.dueDate ? bill.dueDate.slice(5) : ''),
            icon: 'warn',
            prompt: '查一下信用卡账单，如果余额够就帮我还款',
            detail: {
              headline: daysToDue <= 3
                ? '该账单 ' + (daysToDue === 0 ? '今日' : daysToDue + ' 天内') + '到期，逾期将影响征信并产生利息'
                : '本期账单已出，建议在到期日前完成还款',
              metrics: [
                { label: '待还总额', value: '¥' + formatMinor(dueMinor) },
                { label: '最低还款额', value: '¥' + formatMinor(bill && bill.minPaymentMinor || Math.round(dueMinor * 0.1)) },
                { label: '到期日', value: bill && bill.dueDate ? bill.dueDate.slice(0, 10) : '—' },
                { label: '可用额度', value: '¥' + formatMinor(card.availableLimitMinor) },
              ],
              insights: [
                '还款后额度实时恢复，全额还款可享受最长 ' + (bill && bill.interestFreeDays || 20) + ' 天免息期',
                '若暂时资金紧张，可先还最低还款额避免逾期，剩余部分按日计息',
              ],
              suggestion: '活期余额 ¥' + formatMinor(liquidMinor) + ' 足以覆盖本期账单，建议全额还款。',
            },
            actions: [
              { id: 'repay', label: '立即全额还款', kind: 'task', prompt: '帮我还清' + cardName + '尾号 ' + (card.maskedNo || '').slice(-4) + '的全部账单 ¥' + formatMinor(dueMinor) },
              { id: 'bill', label: '查看账单明细', kind: 'catalog', channel: 'home', item: 'repayment', action: 'overview' },
              { id: 'installment', label: '申请账单分期', kind: 'catalog', channel: 'credit', item: 'installment', action: 'apply' },
            ],
          });
        }
      }
    }

    /* ---- 2. 待缴账单提醒 ---- */
    const utilities = await repository.listUtilityBills ? await repository.listUtilityBills(customerId).catch(() => []) : [];
    if (utilities && utilities.length > 0) {
      const unpaid = utilities.filter(u => u.status === 'UNPAID' || u.status === 'unpaid');
      if (unpaid.length > 0) {
        const first = unpaid[0];
        const totalUnpaid = unpaid.reduce((s, u) => s + (u.amountMinor || 0), 0);
        recommendations.push({
          id: 'rec-utility-' + first.id,
          type: 'utility-unpaid',
          priority: 'high',
          title: first.name + '待缴',
          description: (first.period || '') + ' · ¥' + formatMinor(first.amountMinor || 0),
          tag: '未缴',
          icon: 'warn',
          prompt: '交' + first.name,
          detail: {
            headline: '共 ' + unpaid.length + ' 笔生活账单待缴，合计 ¥' + formatMinor(totalUnpaid),
            metrics: unpaid.slice(0, 4).map(u => ({ label: u.name + ' · ' + (u.period || ''), value: '¥' + formatMinor(u.amountMinor || 0) })),
            insights: [
              '欠费超过缴费周期可能产生滞纳金，部分项目会影响正常使用',
            ],
            suggestion: '可用余额 ¥' + formatMinor(liquidMinor) + '，支持一键缴清全部待缴账单。',
          },
          actions: [
            { id: 'pay-all', label: '一键缴清 ' + unpaid.length + ' 笔', kind: 'task', prompt: '帮我把所有待缴的生活账单都交了' },
            { id: 'list', label: '查看待缴列表', kind: 'catalog', channel: 'home', item: 'utility', action: 'overview' },
          ],
        });
      }
    }

    /* ---- 3. 发薪理财建议（来自 wealthService 推荐引擎） ---- */
    if (wealthRecos && wealthRecos.items && wealthRecos.items.length > 0) {
      const top = wealthRecos.items[0];
      const rate = top.annualRateBp ? (top.annualRateBp / 100).toFixed(2) + '%' : '—';
      recommendations.push({
        id: 'rec-wealth-salary',
        type: 'salary-invest',
        priority: 'medium',
        title: '发薪日智能理财建议',
        description: top.reason || ('建议配置 ' + (top.productName || '理财产品') + '，预期年化 ' + rate),
        tag: 'AI 推荐',
        icon: 'info',
        prompt: '分析一下我的资产配置，给我理财建议',
        detail: {
          headline: top.reason || '基于你的收支画像，推荐将部分闲置资金配置现金管理类产品',
          metrics: [
            { label: '推荐产品', value: top.productName || '龙钱宝' },
            { label: '业绩比较基准', value: rate },
            { label: '建议转入', value: top.suggestedAmountMinor ? '¥' + formatMinor(top.suggestedAmountMinor) : '活期余额的 30%-50%' },
            { label: '风险等级', value: top.riskLevel || 'R1 低风险' },
          ],
          insights: [
            '现金管理类产品支持快速赎回，兼顾收益与流动性',
            '设置发薪日自动转入，可避免资金闲置，养成攒钱习惯',
            '按 ' + rate + ' 测算，每万元每日收益约 ' + (top.annualRateBp ? (top.annualRateBp / 100 / 365 * 100).toFixed(2) + ' 元' : '—'),
          ],
          suggestion: '你每月 ' + (salaryInsights && salaryInsights.monthlyAvgSalary ? salaryInsights.monthlyAvgSalary.display : '工资') + '，建议将发薪日次日自动转入，不影响日常支付。',
        },
        actions: [
          { id: 'product', label: '查看产品详情', kind: 'catalog', channel: 'wealth', item: 'dragon-money', action: 'overview' },
          { id: 'buy', label: '立即申购', kind: 'catalog', channel: 'wealth', item: 'dragon-money', action: 'buy' },
          { id: 'auto', label: '设置发薪自动攒', kind: 'catalog', channel: 'wealth', item: 'dragon-money', action: 'auto-invest' },
        ],
      });
    }

    /* ---- 4. 资产配置洞察 ---- */
    if (holdings && holdings.length > 0 && salaryInsights) {
      const totalWealthMinor = holdings.reduce((s, h) => s + (h.currentValueMinor || 0), 0);
      const cashRatio = liquidMinor > 0 ? liquidMinor / (liquidMinor + totalWealthMinor) : 0;
      if (cashRatio > 0.6) {
        const investableMinor = liquidMinor - Math.round((salaryInsights.monthlyAvgExpense ? salaryInsights.monthlyAvgExpense.amountMinor : 0) * 6);
        recommendations.push({
          id: 'rec-wealth-rebalance',
          type: 'wealth-rebalance',
          priority: 'medium',
          title: '活期资金占比偏高',
          description: '活期占比 ' + (cashRatio * 100).toFixed(0) + '%，建议将部分闲置资金转入理财，提升整体收益',
          tag: '资产优化',
          icon: 'info',
          prompt: '分析一下我的资产配置，给我优化建议',
          detail: {
            headline: '活期资金占比 ' + (cashRatio * 100).toFixed(0) + '%，超出 6 个月应急备用线，存在收益损耗',
            metrics: [
              { label: '活期资金', value: '¥' + formatMinor(liquidMinor) },
              { label: '理财持仓', value: '¥' + formatMinor(totalWealthMinor) },
              { label: '月均支出', value: salaryInsights.monthlyAvgExpense ? salaryInsights.monthlyAvgExpense.display : '—' },
              { label: '可优化资金', value: investableMinor > 0 ? '¥' + formatMinor(investableMinor) : '—' },
            ],
            insights: [
              '应急备用金建议保留 3-6 个月支出（约 ¥' + formatMinor((salaryInsights.monthlyAvgExpense ? salaryInsights.monthlyAvgExpense.amountMinor : 0) * 6) + '）',
              '超出部分转入现金理财，按 1.5% 测算每年可多赚 ¥' + formatMinor(Math.max(investableMinor, 0) * 0.015),
              '当前持有 ' + holdings.length + ' 只产品，可关注持仓集中度',
            ],
            suggestion: '建议先保留应急资金，其余分批转入龙钱宝等现金管理产品，保持流动性同时提升收益。',
          },
          actions: [
            { id: 'overview', label: '查看资产全景', kind: 'catalog', channel: 'wealth', item: 'overview', action: 'overview' },
            { id: 'plan', label: '给我配置方案', kind: 'task', prompt: '基于我的活期和持仓情况，给我一份具体的资产配置方案' },
            { id: 'buy', label: '转入龙钱宝', kind: 'catalog', channel: 'wealth', item: 'dragon-money', action: 'buy' },
          ],
        });
      }
    }

    /* ---- 5. 消费洞察 ---- */
    if (salaryInsights && salaryInsights.monthlyAvgExpense) {
      const monthlyExpense = salaryInsights.monthlyAvgExpense.amountMinor || 0;
      const monthlySalary = salaryInsights.monthlyAvgSalary ? salaryInsights.monthlyAvgSalary.amountMinor : 0;
      if (monthlySalary > 0 && monthlyExpense > monthlySalary * 0.8) {
        const ratio = (monthlyExpense / monthlySalary * 100).toFixed(0);
        recommendations.push({
          id: 'rec-spending-insight',
          type: 'spending-insight',
          priority: 'medium',
          title: '本月消费占收入比偏高',
          description: '近 3 月平均支出 ¥' + formatMinor(monthlyExpense) + '，占收入 ' + ratio + '%，建议关注消费结构',
          tag: '消费提醒',
          icon: 'info',
          prompt: '分析我的消费情况',
          detail: {
            headline: '近 3 月平均月支出为月均收入的 ' + ratio + '%，储蓄率偏低，建议控制非必要开支',
            metrics: [
              { label: '月均支出', value: '¥' + formatMinor(monthlyExpense) },
              { label: '月均收入', value: '¥' + formatMinor(monthlySalary) },
              { label: '月度缺口', value: '¥' + formatMinor(monthlyExpense - monthlySalary) },
              { label: '支出收入比', value: ratio + '%' },
            ],
            insights: [
              '按当前支出水平，每月缺口 ¥' + formatMinor(monthlyExpense - monthlySalary) + '，长期将侵蚀存量资金',
              '建议将支出收入比控制在 70% 以内，月支出目标 ¥' + formatMinor(monthlySalary * 0.7),
              '可开启嗖嗖攒，消费后自动小额攒钱，抑制非必要消费',
            ],
            suggestion: '优先梳理大额固定支出，削减可选项订阅与冲动消费，并把结余自动转入理财。',
          },
          actions: [
            { id: 'analysis', label: '查看收支分析', kind: 'catalog', channel: 'me', item: 'income-expense', action: 'overview' },
            { id: 'advice', label: '帮我找出可省的开支', kind: 'task', prompt: '分析我最近的消费流水，找出可以削减的非必要开支' },
            { id: 'save', label: '开启嗖嗖攒', kind: 'catalog', channel: 'wealth', item: 'save', action: 'setup' },
          ],
        });
      }
    }

    /* ---- 6. 贷款还款提醒 ---- */
    if (loans && loans.data && loans.data.length > 0) {
      for (const loan of loans.data) {
        if (loan.status !== 'ACTIVE') continue;
        const nextPayment = loan.nextPaymentDate || (loan.repaymentSchedule && loan.repaymentSchedule[0] && loan.repaymentSchedule[0].dueDate);
        if (nextPayment) {
          const daysToPayment = Math.ceil((new Date(nextPayment) - new Date(now())) / 86400000);
          if (daysToPayment <= 7 && daysToPayment >= 0) {
            recommendations.push({
              id: 'rec-loan-' + loan.id,
              type: 'loan-payment',
              priority: daysToPayment <= 3 ? 'high' : 'medium',
              title: '贷款还款日临近',
              description: (loan.productName || '贷款') + ' · 月供 ¥' + formatMinor(loan.monthlyPaymentMinor || 0) + ' · ' + (daysToPayment === 0 ? '今日' : daysToPayment + ' 天后') + '扣款',
              tag: '还款提醒',
              icon: 'warn',
              prompt: '查看贷款还款计划',
              detail: {
                headline: (loan.productName || '贷款') + ' 将于 ' + (daysToPayment === 0 ? '今日' : daysToPayment + ' 天后') + '自动扣款',
                metrics: [
                  { label: '本期月供', value: '¥' + formatMinor(loan.monthlyPaymentMinor || 0) },
                  { label: '扣款日期', value: String(nextPayment).slice(0, 10) },
                  { label: '剩余本金', value: loan.outstandingPrincipalMinor != null ? '¥' + formatMinor(loan.outstandingPrincipalMinor) : '—' },
                  { label: '扣款账户余额', value: '¥' + formatMinor(liquidMinor) },
                ],
                insights: [
                  '请确保扣款账户在扣款日前有足额资金，避免逾期影响征信',
                  '活期余额 ¥' + formatMinor(liquidMinor) + (liquidMinor >= (loan.monthlyPaymentMinor || 0) ? '，覆盖本期月供无压力' : '，不足以覆盖月供，请及时转入'),
                ],
                suggestion: '如资金充裕，可考虑部分提前还款以减少利息支出。',
              },
              actions: [
                { id: 'plan', label: '查看还款计划', kind: 'catalog', channel: 'home', item: 'loan', action: 'overview' },
                { id: 'prepay', label: '提前还款试算', kind: 'task', prompt: '帮我试算一下提前还款 ' + (loan.productName || '贷款') + ' 能省多少利息' },
              ],
            });
          }
        }
      }
    }

    /* ---- 7. 积分到期提醒 ---- */
    const points = await repository.getCustomerPoints ? await repository.getCustomerPoints(customerId).catch(() => null) : null;
    if (points && points.expiringAmount > 0) {
      recommendations.push({
        id: 'rec-points-expiry',
        type: 'point-expiry',
        priority: 'low',
        title: '龙积分即将到期',
        description: points.expiringAmount + ' 积分将于 ' + (points.expiryDate || '本月底') + ' 过期，可在积分商城兑换礼品',
        tag: '积分提醒',
        icon: 'info',
        prompt: '查看我的龙积分',
        detail: {
          headline: points.expiringAmount + ' 积分即将过期，过期后自动清零不可恢复',
          metrics: [
            { label: '可用积分', value: (points.balance != null ? points.balance : points.expiringAmount) + ' 分' },
            { label: '即将过期', value: points.expiringAmount + ' 分' },
            { label: '过期时间', value: points.expiryDate || '本月底' },
            { label: '可兑换', value: '约 ¥' + (points.expiringAmount / 1000).toFixed(0) + ' 权益' },
          ],
          insights: [
            '积分可在积分商城兑换话费、视频会员、立减金等权益',
            '部分信用卡消费有多倍积分活动，日常消费可优先使用',
          ],
          suggestion: '建议尽快使用即将过期积分，优先兑换刚需权益。',
        },
        actions: [
          { id: 'exchange', label: '去积分商城兑换', kind: 'catalog', channel: 'credit', item: 'points', action: 'apply' },
          { id: 'detail', label: '查看积分明细', kind: 'catalog', channel: 'credit', item: 'points', action: 'overview' },
        ],
      });
    }

    // 按优先级排序
    const priorityOrder = { critical: 0, high: 1, medium: 2, low: 3 };
    recommendations.sort((a, b) => (priorityOrder[a.priority] || 3) - (priorityOrder[b.priority] || 3));

    return {
      customerId,
      recommendations,
      summary: {
        total: recommendations.length,
        critical: recommendations.filter(r => r.priority === 'critical').length,
        high: recommendations.filter(r => r.priority === 'high').length,
      },
      profile: {
        liquidAssets: moneyValue(liquidMinor, 'CNY'),
        monthlyAvgSalary: salaryInsights ? salaryInsights.monthlyAvgSalary : null,
        monthlyAvgExpense: salaryInsights ? salaryInsights.monthlyAvgExpense : null,
        wealthHoldingsCount: holdings ? holdings.length : 0,
        activeLoansCount: loans && loans.data ? loans.data.filter(l => l.status === 'ACTIVE').length : 0,
        activeCardsCount: cards ? cards.filter(c => c.status === 'ACTIVE').length : 0,
      },
      generatedAt: now(),
      source: 'daily-agent-task',
      disclaimer: '以上推荐基于您的账户数据由智能体每日生成，仅供参考。',
    };
  }

  async function getDailyRecommendations(customerId) {
    if (!nextActionService) return getLegacyDailyRecommendations(customerId);
    const useUnifiedList = typeof nextActionService.list === 'function';
    const [result, legacyResult] = await Promise.all([
      useUnifiedList
        ? nextActionService.list(customerId, { surface: 'home', channel: null, limit: 3 })
        : nextActionService.listCompatibility(customerId),
      repository && bankService
        ? getLegacyDailyRecommendations(customerId).catch(() => null)
        : Promise.resolve(null),
    ]);
    const richRecommendationsByType = new Map();
    for (const recommendation of legacyResult ? legacyResult.recommendations : []) {
      const queue = richRecommendationsByType.get(recommendation.type) || [];
      queue.push(recommendation);
      richRecommendationsByType.set(recommendation.type, queue);
    }
    const scenarios = new Map(((experienceRegistry && experienceRegistry.scenarios) || [])
      .map(scenario => [scenario.id, scenario]));
    const recommendations = await Promise.all(result.items.map(async item => {
      const scenario = scenarios.get(item.scenarioId) || null;
      const legacyType = item.legacyType || (scenario && scenario.legacyType) || null;
      const queue = richRecommendationsByType.get(legacyType) || [];
      const legacyRich = queue.shift() || null;
      const artifact = useUnifiedList && artifactProvider && typeof artifactProvider.build === 'function'
        ? await artifactProvider.build(customerId, item).catch(() => null)
        : null;
      return projectLegacyRecommendation(item, artifact, legacyRich, useUnifiedList, legacyType);
    }));
    return {
      customerId,
      recommendations,
      summary: {
        total: recommendations.length,
        critical: recommendations.filter(item => item.priority === 'critical').length,
        high: recommendations.filter(item => item.priority === 'high').length,
      },
      profile: legacyResult ? legacyResult.profile : null,
      generatedAt: result.generatedAt,
      source: useUnifiedList ? 'next-action-home' : 'next-action-compatibility',
      disclaimer: '以上推荐基于您的账户数据生成，仅供参考。',
    };
  }

  function projectLegacyRecommendation(item, artifact, richRecommendation, trustedAction, legacyType) {
    const priority = item.priority === 'normal' ? 'medium' : item.priority;
    const catalogAction = item.catalogAction;
    const rich = richRecommendation || null;
    const detail = artifact ? artifact.detail : rich ? rich.detail : {
      headline: item.reasonText,
      metrics: [],
      insights: [],
      suggestion: item.reasonText,
    };
    let actions;
    if (trustedAction && item.action === 'START_ASSISTANCE') {
      actions = [{
        id: 'start-assistance',
        label: artifact ? artifact.primaryActionLabel : '开始办理',
        kind: 'recommendation',
        recommendationId: item.recommendationId,
      }];
    } else if (trustedAction && item.action === 'OPEN_CATALOG' && catalogAction) {
      actions = [{
        id: 'open',
        label: (artifact && artifact.primaryActionLabel) || '查看服务',
        kind: 'catalog',
        channel: catalogAction.channel, item: catalogAction.item, action: catalogAction.action,
      }];
    } else {
      actions = rich ? rich.actions : (catalogAction ? [{
        id: 'open',
        label: (artifact && artifact.primaryActionLabel) || '查看服务',
        kind: 'catalog',
        channel: catalogAction.channel, item: catalogAction.item, action: catalogAction.action,
      }] : []);
    }
    return {
      id: item.recommendationId,
      type: item.type || legacyType || 'proactive_service',
      priority,
      title: artifact ? artifact.card.title : item.title,
      description: artifact ? artifact.card.summary : item.reasonText,
      amount: artifact ? artifact.card.amount : null,
      safetyHint: artifact ? artifact.card.safetyHint : null,
      tag: rich ? rich.tag : (priority === 'critical' || priority === 'high' ? '待处理' : 'AI 推荐'),
      icon: rich ? rich.icon : (priority === 'critical' || priority === 'high' ? 'warn' : 'info'),
      prompt: trustedAction ? item.title : rich ? rich.prompt : item.title,
      detail,
      actions,
    };
  }

  function formatMinor(minor) {
    if (!minor || minor <= 0) return '0.00';
    return (minor / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  return { getDailyRecommendations };
}

module.exports = { createRecommendationService };
