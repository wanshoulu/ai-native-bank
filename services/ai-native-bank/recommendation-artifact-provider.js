'use strict';

const { SPENDING_CATEGORY_LABELS } = require('./spending-category-labels');

function createRecommendationArtifactProvider(options = {}) {
  const cashflowService = options.cashflowService;
  const obligationService = options.obligationService;
  const signalService = options.signalService;
  // 生活优惠卡读取商户活动列表；可选依赖（旧测试构造可不传，offers 卡运行时降级）
  const repository = options.repository || null;
  // 理财到期卡读取真实持仓；可选依赖（未注入时该场景降级为默认文案）
  const wealthService = options.wealthService || null;
  const now = typeof options.now === 'function' ? options.now : () => new Date().toISOString();
  if (!cashflowService || typeof cashflowService.getSalaryPlan !== 'function'
    || typeof cashflowService.getSpendingAnalysis !== 'function'
    || !obligationService || typeof obligationService.list !== 'function'
    || !signalService || typeof signalService.collect !== 'function') {
    throw new TypeError('recommendation artifact provider dependencies are invalid');
  }

  // 构建器按 scenarioId 路由：artifact 是场景所有的内容投影。此前按 targetIntentId
  // 路由，多个场景共享同一意图域时（如 loan-payment 与 obligation-repayment 同属
  // ccb.obligation.repay-all）会被误路由到别的场景卡片，产生标题冒用的重复卡。
  const builders = new Map([
    ['salary-monthly-plan', buildSalaryPlan],
    ['obligation-repayment', buildObligationPlan],
    ['spending-explanation', buildSpendingAnalysis],
    ['care-morning-greeting', buildCareGreeting],
    ['care-evening-greeting', buildCareGreeting],
    ['care-budget-progress', buildBudgetProgress],
    ['life-weather-rain', buildWeatherCard],
    ['life-weather-coldsnap', buildWeatherCard],
    ['life-weather-weekend-outing', buildWeatherCard],
    ['promo-offers-expiring', buildOffersCard],
    ['promo-offers-dining', buildOffersCard],
    ['life-wealth-maturing', buildWealthMaturing],
  ]);
  if (options.builders !== undefined) {
    if (!options.builders || typeof options.builders !== 'object' || Array.isArray(options.builders)) {
      throw new TypeError('recommendation artifact builders are invalid');
    }
    Object.entries(options.builders).forEach(([scenarioId, builder]) => {
      requireId(scenarioId, 'scenarioId');
      if (typeof builder !== 'function') throw new TypeError('recommendation artifact builder is invalid');
      builders.set(scenarioId, builder);
    });
  }

  async function build(customerId, recommendation) {
    requireId(customerId, 'customerId');
    if (!recommendation || typeof recommendation !== 'object' || Array.isArray(recommendation)) {
      throw new TypeError('recommendation is invalid');
    }
    const builder = builders.get(recommendation.scenarioId);
    if (!builder) return null;
    const value = await builder({ customerId, recommendation, now: requireTime(now()) });
    validateArtifact(value);
    return deepFreeze(value);
  }

  async function buildSalaryPlan({ customerId }) {
    const plan = await cashflowService.getSalaryPlan(customerId);
    const salaryDisplay = formatMoney(plan.salary.amountMinor);
    const obligationItems = Array.isArray(plan.obligationItems) ? plan.obligationItems : [];
    const obligationCount = obligationItems.length;
    const hasGap = plan.uncoveredObligations.amountMinor > 0;
    const supplementaryAccounts = Array.isArray(plan.supplementaryAccounts)
      ? plan.supplementaryAccounts
      : [];
    const supplementaryTotalMinor = supplementaryAccounts.reduce(
      (sum, item) => sum + item.availableMinor, 0,
    );
    const canTopUp = hasGap && supplementaryTotalMinor > 0;
    // 记忆来源标注（边界红线）：记忆值只当参谋，来源在 items.sourceRefs（memory:*）可见。
    const memorySourced = id => (((plan.items || []).find(item => item.id === id)) || { sourceRefs: [] })
      .sourceRefs.some(ref => typeof ref === 'string' && ref.startsWith('memory:'));
    const insights = [
      `待还合计 ${formatMoney(plan.totalObligations.amountMinor)}，由 ${obligationCount} 笔信用卡、贷款或生活账单组成。`,
      `当前可用余额 ${formatMoney(plan.availableBalance.amountMinor)}。`,
      `已先保留生活预算 ${formatMoney(plan.livingBudget.amountMinor)}${memorySourced('living_budget') ? '（按你设定）' : ''}。`,
      `已先保留应急资金 ${formatMoney(plan.emergencyReserve.amountMinor)}${memorySourced('emergency_reserve') ? '（按你设定）' : ''}。`,
    ];
    supplementaryAccounts.slice(0, 4).forEach(item => {
      insights.push(`${item.accountName}可用 ${formatMoney(item.availableMinor)}，可作为资金缺口的补足来源。`);
    });
    let suggestion;
    if (canTopUp) {
      const names = supplementaryAccounts.slice(0, 3).map(item => item.accountName).join('、');
      suggestion = `本次可安排还款 ${formatMoney(plan.plannedRepayment.amountMinor)}，仍有 ${formatMoney(plan.uncoveredObligations.amountMinor)} 缺口；可将${names}资金转入付款账户后安排还款。`;
    } else if (hasGap) {
      suggestion = `本次可安排还款 ${formatMoney(plan.plannedRepayment.amountMinor)}，仍有 ${formatMoney(plan.uncoveredObligations.amountMinor)} 需要调整或后续处理。`;
    } else {
      suggestion = '当前资金可以覆盖全部待还事项，涉及实际扣款时仍需逐项确认。';
    }
    return {
      card: {
        title: '本月工资已到账，可以开始安排',
        summary: `待还合计 ${formatMoney(plan.totalObligations.amountMinor)} · 本次可安排还款 ${formatMoney(plan.plannedRepayment.amountMinor)}`,
        amount: money(plan.salary.amountMinor),
        safetyHint: '方案可以调整，涉及资金操作时仍需逐项确认',
      },
      detail: {
        headline: `${obligationCount} 笔待还合计 ${formatMoney(plan.totalObligations.amountMinor)}，已结合当前余额保留生活预算和应急资金`,
        metrics: [
          metric('本月工资', salaryDisplay),
          metric('付款账户可用', formatMoney(plan.availableBalance.amountMinor)),
          metric('待还合计', formatMoney(plan.totalObligations.amountMinor)),
          metric('本次可安排还款', formatMoney(plan.plannedRepayment.amountMinor)),
          metric(hasGap ? '仍有资金缺口' : '还款后可用', formatMoney(
            hasGap ? plan.uncoveredObligations.amountMinor : plan.allocatable.amountMinor,
          )),
          metric('生活预算', formatMoney(plan.livingBudget.amountMinor)),
          metric('应急资金', formatMoney(plan.emergencyReserve.amountMinor)),
          metric('可配置资金', formatMoney(plan.allocatable.amountMinor)),
        ],
        items: obligationItems.map(item => {
          const executable = item.executionStatus === 'available';
          const affordable = item.amountMinor <= plan.availableBalance.amountMinor;
          const needsBudgetAdjustment = item.amountMinor > plan.plannedRepayment.amountMinor;
          return {
            id: item.id,
            type: item.type,
            title: item.displayName,
            subtitle: formatDueDate(item.dueAt),
            amount: money(item.amountMinor),
            availability: !executable
              ? { state: 'unavailable', reason: '当前信息暂不支持直接办理' }
              : !affordable
                ? { state: 'needs_information', reason: '当前付款账户余额不足，需先调整资金安排' }
                : { state: 'available', reason: null },
            action: executable && affordable
              ? buildObligationAction(item, { needsBudgetAdjustment }) : null,
          };
        }),
        insights,
        suggestion,
      },
      primaryActionLabel: '调整资金安排',
    };
  }

  async function buildObligationPlan({ customerId }) {
    const obligations = await obligationService.list(customerId);
    const counts = { credit_card: 0, loan: 0, utility: 0 };
    obligations.items.forEach(item => { if (Object.hasOwn(counts, item.type)) counts[item.type] += 1; });
    const availableMinor = obligations.availableBalance.amountMinor;
    const remainingMinor = availableMinor - obligations.total.amountMinor;
    const sufficient = remainingMinor >= 0;
    return {
      card: {
        title: `近期有 ${obligations.items.length} 笔账单待处理`,
        summary: `信用卡 ${counts.credit_card} 笔 · 贷款 ${counts.loan} 笔 · 生活缴费 ${counts.utility} 笔`,
        amount: money(obligations.total.amountMinor),
        safetyHint: '实际扣款前将由你逐项确认',
      },
      detail: {
        headline: sufficient
          ? `默认付款账户余额足以覆盖全部 ${obligations.items.length} 笔账单`
          : `默认付款账户仍有 ${formatMoney(Math.abs(remainingMinor))} 资金缺口`,
        metrics: [
          metric('待处理合计', formatMoney(obligations.total.amountMinor)),
          metric('付款账户可用', formatMoney(availableMinor)),
          metric(sufficient ? '处理后预计剩余' : '预计资金缺口', formatMoney(Math.abs(remainingMinor))),
        ],
        items: obligations.items.map(item => ({
          id: item.id,
          type: item.type,
          title: item.displayName,
          subtitle: formatDueDate(item.dueAt),
          amount: money(item.amount.amountMinor),
          availability: item.executionStatus === 'available'
            ? { state: 'available', reason: null }
            : { state: 'unavailable', reason: '当前信息暂不支持直接办理' },
          action: item.executionStatus === 'available' ? buildObligationAction(item) : null,
        })),
        insights: [sufficient
          ? '默认付款账户余额足以覆盖全部账单，可按到期时间依次办理。'
          : '当前可用余额不足以覆盖全部账单，建议先处理临近到期项目。'],
        suggestion: '建议优先处理临近到期项目。',
        callToAction: {
          message: `我可以帮你一次安排这 ${obligations.items.length} 笔账单，现在开始吗？`,
          primaryLabel: '一键办理',
          activationPhrases: ['一键办理', '全部办理', '全部帮我处理'],
          confirmationHint: '将先核对办理顺序和金额，实际扣款前会逐项请你确认。',
        },
      },
      primaryActionLabel: '一键办理',
    };
  }

  async function buildSpendingAnalysis({ customerId, recommendation, now: currentTime }) {
    const snapshot = await signalService.collect({
      customerId,
      surface: recommendation.surface || 'home',
      channel: recommendation.channel || null,
      personalizationEnabled: true,
    });
    const periodRecord = snapshot && snapshot.signals && snapshot.signals['input.period'];
    if (!periodRecord || !/^\d{4}-(0[1-9]|1[0-2])$/.test(periodRecord.value || '')
      || !Number.isFinite(Date.parse(periodRecord.expiresAt))
      || Date.parse(periodRecord.expiresAt) <= Date.parse(currentTime)) {
      throw new Error('recommendation spending period is unavailable');
    }
    const analysis = await cashflowService.getSpendingAnalysis(customerId, { period: periodRecord.value });
    const delta = analysis.current.netExpenseMinor - analysis.previous.netExpenseMinor;
    // 类别归因明细：把分析结论落到可读的类别行上，兑现「查看完整归因」的承诺。
    const categories = Array.isArray(analysis.categories) ? analysis.categories : [];
    const driverLabels = categories
      .filter(item => item.deltaMinor !== 0 && Math.sign(item.deltaMinor) === Math.sign(delta))
      .slice(0, 2)
      .map(item => SPENDING_CATEGORY_LABELS[item.category] || item.category);
    const driverInsight = driverLabels.length
      ? `${delta < 0 ? '支出减少' : '支出增加'}主要来自${driverLabels.join('、')}。`
      : null;
    return {
      card: {
        title: delta > 0 ? '本月支出较上月有所增加'
          : delta < 0 ? '本月支出较上月有所减少' : '本月支出与上月基本持平',
        summary: `${analysis.period} 净支出 ${formatMoney(analysis.current.netExpenseMinor)} · 环比 ${formatSignedMoney(delta)}`,
        amount: money(analysis.current.netExpenseMinor),
        safetyHint: null,
      },
      detail: {
        headline: `已完成 ${analysis.period} 与 ${analysis.comparisonPeriod} 的可追溯支出对比`,
        metrics: [
          metric('本月净支出', formatMoney(analysis.current.netExpenseMinor)),
          metric('上月净支出', formatMoney(analysis.previous.netExpenseMinor)),
          metric('环比变化', formatSignedMoney(delta)),
          metric('已排除内部转账', formatMoney(analysis.excluded.internalTransferMinor)),
          metric('已冲抵退款', formatMoney(analysis.excluded.refundMinor)),
        ],
        items: categories
          .filter(item => item.currentMinor >= 0)
          .slice(0, 5)
          .map(item => ({
            id: `category.${item.category}`,
            title: SPENDING_CATEGORY_LABELS[item.category] || item.category,
            subtitle: `${analysis.period} 净支出`,
            amount: money(item.currentMinor),
            status: item.deltaMinor === 0
              ? '与上月持平'
              : `较上月 ${formatSignedMoney(item.deltaMinor)}`,
          })),
        insights: [
          ...(driverInsight ? [driverInsight] : []),
          delta > 0 ? '本月净支出高于上月。' : delta < 0 ? '本月净支出低于上月。' : '本月净支出与上月持平。',
        ],
        suggestion: '可继续查看支出变化对应的明细依据和完整归因。',
      },
      primaryActionLabel: '查看完整归因',
    };
  }

  // 关怀问候卡：内容不是固定文案，而是从当次信号快照里挑一条对用户此刻有用的
  // 提示（临近账单、发薪倒计时、支出节奏），实现"个性化关怀"而非模板化问候。
  async function buildCareGreeting({ customerId, recommendation }) {
    const snapshot = await signalService.collect({
      customerId,
      surface: recommendation.surface || 'home',
      channel: recommendation.channel || null,
      personalizationEnabled: true,
    });
    const read = id => (snapshot.signals[id] ? snapshot.signals[id].value : null);
    const dueCount = read('obligation.due_count');
    const daysToSalary = read('salary.days_until_credit');
    const ratio = read('spending.expense_income_ratio');
    const evening = recommendation.scenarioId === 'care-evening-greeting';
    const tips = [];
    if (Number.isInteger(dueCount) && dueCount > 0) {
      tips.push(`目前有 ${dueCount} 笔账单待处理，可安排时间逐项确认。`);
    }
    if (Number.isInteger(daysToSalary) && daysToSalary > 0 && daysToSalary <= 31) {
      tips.push(`距离下次工资到账约 ${daysToSalary} 天。`);
    }
    if (typeof ratio === 'number' && ratio > 0.8) {
      tips.push('本月支出节奏偏快，可在收支明细里看看主要去向。');
    }
    if (!tips.length) tips.push('近期没有临近到期的账单，资金安排可以从容一些。');
    return {
      card: {
        title: evening ? '晚上好，回顾今天的收支' : '早安，今天也安心开局',
        summary: tips[0],
        amount: null,
        safetyHint: null,
      },
      detail: {
        headline: tips[0],
        metrics: [
          ...(Number.isInteger(dueCount) ? [metric('待处理账单', `${dueCount} 笔`)] : []),
          ...(Number.isInteger(daysToSalary) && daysToSalary <= 31
            ? [metric('距下次发薪', `${daysToSalary} 天`)] : []),
          ...(typeof ratio === 'number' ? [metric('支出占收入比', `${Math.round(ratio * 100)}%`)] : []),
        ],
        items: [],
        insights: tips,
        suggestion: evening ? '回顾今日收支，安心休息。' : '可查看本月收支概览，从容安排今天。',
      },
      primaryActionLabel: '查看收支概览',
    };
  }

  // 预算关怀卡：把记忆中的生活预算（monthly_living_budget，来源标注"按你设定"）
  // 与当月实际支出对比，形成进度提醒。无预算数据时展示为 0 并退化为支出概览。
  async function buildBudgetProgress({ customerId }) {
    const [plan, analysis] = await Promise.all([
      cashflowService.getSalaryPlan(customerId),
      cashflowService.getSpendingAnalysis(customerId, { period: requireTime(now()).slice(0, 7) }),
    ]);
    const budgetMinor = plan.livingBudget.amountMinor;
    const spentMinor = analysis.current.netExpenseMinor;
    const percent = budgetMinor > 0 ? Math.min(999, Math.round((spentMinor / budgetMinor) * 100)) : 0;
    const memorySourced = (((plan.items || []).find(item => item.id === 'living_budget'))
      || { sourceRefs: [] }).sourceRefs.some(ref => typeof ref === 'string' && ref.startsWith('memory:'));
    const remainingMinor = budgetMinor - spentMinor;
    const stateText = budgetMinor <= 0
      ? '尚未设定生活预算，可在工资规划中设置。'
      : remainingMinor >= 0
        ? `按你${memorySourced ? '设定' : '默认'}的生活预算，本月节奏正常。`
        : '本月支出已超过生活预算，建议查看主要去向。';
    return {
      card: {
        title: budgetMinor > 0 ? `本月生活预算已使用约 ${percent}%` : '本月生活支出概览',
        summary: budgetMinor > 0
          ? `预算 ${formatMoney(budgetMinor)} · 已支出 ${formatMoney(spentMinor)}`
          : `本月净支出 ${formatMoney(spentMinor)}`,
        amount: money(Math.max(0, spentMinor)),
        safetyHint: null,
      },
      detail: {
        headline: stateText,
        metrics: [
          metric('生活预算', formatMoney(budgetMinor)),
          metric('本月已支出', formatMoney(spentMinor)),
          metric(remainingMinor >= 0 ? '预算剩余' : '超出预算', formatMoney(Math.abs(remainingMinor))),
        ],
        items: [],
        insights: [stateText],
        suggestion: remainingMinor >= 0
          ? '保持当前节奏，月底前可再核对一次。'
          : '可查看支出归因，判断是否需要调整消费或预算。',
      },
      primaryActionLabel: '查看收支概览',
    };
  }

  // 天气生活提示卡：读 life.* 信号，按场景分支文案；condition→文案用通用映射，
  // 新天气形态只需扩展映射表，不加分支特判。
  async function buildWeatherCard({ customerId, recommendation }) {
    const snapshot = await signalService.collect({
      customerId,
      surface: recommendation.surface || 'home',
      channel: recommendation.channel || null,
      personalizationEnabled: true,
    });
    const read = id => (snapshot.signals[id] ? snapshot.signals[id].value : null);
    const condition = read('life.weather_condition');
    const tempHigh = read('life.weather_temp_high');
    const tempText = Number.isInteger(tempHigh) ? `最高 ${tempHigh}°C` : null;
    const CONDITION_LABEL = { rain: '雨', snow: '雪', clear: '晴', cloudy: '多云', unknown: '暂无天气数据' };
    const conditionLabel = CONDITION_LABEL[condition] || condition;
    const scene = {
      'life-weather-rain': {
        headline: condition === 'snow' ? '今天有雪，出行注意防滑保暖。' : '今天有雨，出门记得带伞，路面湿滑注意安全。',
        metrics: [metric('天气', `${conditionLabel}${tempText ? ' · ' + tempText : ''}`)],
        insights: ['雨天出行建议预留更多时间，优先室内行程。'],
        suggestion: '可查看出行服务，规划今天的行程。',
        actionLabel: '查看出行服务',
      },
      'life-weather-coldsnap': {
        headline: `今天最高气温仅 ${tempHigh}°C，注意添衣保暖。`,
        metrics: [metric('最高气温', `${tempHigh}°C`)],
        insights: ['气温骤降易感冒，老人与儿童减少清晨外出。'],
        suggestion: '可查看医疗健康服务，了解就近药房与门诊。',
        actionLabel: '查看医疗健康',
      },
      'life-weather-weekend-outing': {
        headline: '周末天气不错，适合出门走走。',
        metrics: [metric('天气', `${conditionLabel}${tempText ? ' · ' + tempText : ''}`)],
        insights: ['好天气适合近郊出游，自驾可留意加油优惠。'],
        suggestion: '可查看车主服务，加油停车一站搞定。',
        actionLabel: '查看车主服务',
      },
    }[recommendation.scenarioId];
    if (!scene) throw new TypeError('weather scenario is invalid');
    return {
      card: {
        title: recommendation.title || '今日天气提示',
        summary: scene.headline,
        amount: null,
        safetyHint: null,
      },
      detail: {
        headline: scene.headline,
        metrics: scene.metrics,
        items: [],
        insights: scene.insights,
        suggestion: scene.suggestion,
      },
      primaryActionLabel: scene.actionLabel,
    };
  }

  // 优惠活动卡：读 repository 优惠列表，按场景筛"临期"/"美食"两类。数据不可用时
  // 抛错由 focus 层降级为推荐默认文案，不阻断首页。
  async function buildOffersCard({ customerId, recommendation }) {
    const offers = repository && typeof repository.listActiveOffers === 'function'
      ? await repository.listActiveOffers() : null;
    if (!Array.isArray(offers)) throw new TypeError('offers repository is unavailable');
    const expiringOnly = recommendation.scenarioId === 'promo-offers-expiring';
    const picked = offers
      .filter(item => expiringOnly ? (Number.isFinite(item.daysToExpiry) && item.daysToExpiry <= 3) : item.category === 'dining')
      .slice(0, 3);
    if (!picked.length) throw new TypeError('no matching offers');
    const items = picked.map(item => ({
      id: item.id,
      title: item.merchant,
      subtitle: item.title,
      amount: { amountMinor: 0, currency: 'CNY', display: item.discountText },
      status: expiringOnly
        ? (item.daysToExpiry === 0 ? '今天到期' : `${item.daysToExpiry} 天后到期`)
        : `有效期至 ${item.validTo}`,
    }));
    const headline = expiringOnly
      ? `有 ${picked.length} 项优惠即将到期，别错过使用期限。`
      : '有你可用的美食优惠，就餐时可以省一点。';
    return {
      card: {
        title: recommendation.title || '优惠活动',
        summary: picked[0] ? `${picked[0].merchant} ${picked[0].discountText}` : headline,
        amount: null,
        safetyHint: null,
      },
      detail: {
        headline,
        metrics: [metric('可用优惠', `${picked.length} 项`)],
        items,
        insights: expiringOnly ? ['优惠到期后自动失效，可在领券中心查看可领新券。'] : ['优惠由合作商户提供，以页面展示规则为准。'],
        suggestion: '可进入领券中心查看全部活动。',
      },
      primaryActionLabel: expiringOnly ? '查看领券中心' : '查看美食优惠',
    };
  }

  // 理财到期卡：读真实持仓，筛 30 天内到期的项目，落到"哪笔、何时、多少钱"。
  // 持仓服务不可用或没有临近到期项目时抛错，由上层降级为推荐默认文案。
  async function buildWealthMaturing({ customerId, recommendation, now: currentTime }) {
    if (!wealthService || typeof wealthService.listHoldings !== 'function') {
      throw new TypeError('wealth service is unavailable');
    }
    const holdings = await wealthService.listHoldings(customerId);
    const nowMs = Date.parse(currentTime);
    const maturing = (holdings.holdings || [])
      .filter(item => typeof item.maturesAt === 'string' && Number.isFinite(Date.parse(item.maturesAt)))
      .map(item => ({ item, daysLeft: Math.ceil((Date.parse(item.maturesAt) - nowMs) / 86400000) }))
      .filter(row => row.daysLeft >= 0 && row.daysLeft <= 30)
      .sort((left, right) => left.daysLeft - right.daysLeft || left.item.id.localeCompare(right.item.id))
      .slice(0, 5)
      .map(row => ({ ...row.item, daysLeft: row.daysLeft }));
    if (!maturing.length) throw new TypeError('no maturing holdings');
    const maturingValueMinor = maturing.reduce((sum, item) => sum + item.currentValue.amountMinor, 0);
    const daysLabel = daysLeft => (daysLeft === 0 ? '今天到期' : `${daysLeft} 天后到期`);
    const earliest = maturing[0];
    return {
      card: {
        title: recommendation.title || '有理财产品即将到期',
        summary: `${earliest.productName} 等 ${maturing.length} 笔即将到期 · 合计 ${formatMoney(maturingValueMinor)}`,
        amount: money(maturingValueMinor),
        safetyHint: '到期兑付安排以产品说明书为准',
      },
      detail: {
        headline: `有 ${maturing.length} 笔持仓将在 30 天内到期，可提前安排资金衔接`,
        metrics: [
          metric('临近到期', `${maturing.length} 笔`),
          metric('合计市值', formatMoney(maturingValueMinor)),
          metric('最近到期', formatShortDate(earliest.maturesAt)),
        ],
        items: maturing.map(item => ({
          id: `holding.${item.id}`,
          title: item.productName,
          subtitle: `本金 ${item.principal.display} · ${formatShortDate(item.maturesAt)}`,
          amount: money(item.currentValue.amountMinor),
          status: daysLabel(item.daysLeft),
        })),
        insights: [
          `最近一笔「${earliest.productName}」${daysLabel(earliest.daysLeft)}，到期前可评估续接或赎回安排。`,
          '到期资金可继续配置同类产品，或转入现金管理类灵活使用。',
        ],
        suggestion: '可先查看到期持仓确认每笔到期日，再决定续接或转向新的资金安排。',
      },
      primaryActionLabel: '查看到期持仓',
    };
  }

  return Object.freeze({ build });
}

function metric(label, value) { return { label, value }; }
function buildObligationAction(item, options = {}) {
  const subject = String(item.displayName || '').split('·')[0].trim();
  const configured = {
    credit_card: { label: '办理本期还款', phrases: ['还这笔信用卡', `还${subject}`] },
    loan: { label: '办理本期还款', phrases: ['还这期贷款', `还${subject}`] },
    utility: { label: '缴纳本期账单', phrases: [`交这笔${subject}`, `先交${subject}`] },
  }[item.type];
  if (!configured) throw new TypeError('obligation type is invalid');
  if (options.needsBudgetAdjustment) {
    configured.label = item.type === 'utility' ? '调整资金并缴费' : '调整资金并还款';
  }
  return {
    id: `obligation.${item.id}`,
    kind: 'agent_plan',
    label: configured.label,
    targetIntentId: 'ccb.obligation.pay-selected',
    activationPhrases: [...new Set(configured.phrases)],
  };
}
function formatDueDate(value) {
  if (typeof value !== 'string') return '待确认到期日';
  const dateOnly = /^\d{4}-(0[1-9]|1[0-2])-([0-2]\d|3[01])$/.test(value);
  const timestamp = /^\d{4}-(0[1-9]|1[0-2])-([0-2]\d|3[01])T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})$/.test(value);
  if (!dateOnly && !timestamp) return '待确认到期日';
  if (timestamp && !Number.isFinite(Date.parse(value))) return '待确认到期日';
  const datePart = value.slice(0, 10);
  const parsed = new Date(datePart + 'T00:00:00.000Z');
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== datePart) return '待确认到期日';
  return `${parsed.getUTCMonth() + 1}月${parsed.getUTCDate()}日到期`;
}
function money(amountMinor) {
  return { amountMinor, currency: 'CNY', display: formatMoney(amountMinor) };
}
function formatMoney(amountMinor) {
  if (!Number.isSafeInteger(amountMinor)) throw new TypeError('money amount is invalid');
  return `¥${(amountMinor / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
function formatSignedMoney(amountMinor) {
  const prefix = amountMinor > 0 ? '+' : amountMinor < 0 ? '-' : '';
  return prefix + formatMoney(Math.abs(amountMinor));
}
function formatShortDate(value) {
  const datePart = String(value || '').slice(0, 10);
  if (!/^\d{4}-(0[1-9]|1[0-2])-([0-2]\d|3[01])$/.test(datePart)) return '待确认到期日';
  const parsed = new Date(datePart + 'T00:00:00.000Z');
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== datePart) return '待确认到期日';
  return `${parsed.getUTCMonth() + 1}月${parsed.getUTCDate()}日`;
}
function validateArtifact(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || !hasExactKeys(value, ['card', 'detail', 'primaryActionLabel'])
    || !value.card || typeof value.card !== 'object'
    || !hasExactKeys(value.card, ['title', 'summary', 'amount', 'safetyHint'])
    || !isSafeText(value.card.title, 80) || !isSafeText(value.card.summary, 160)
    || (value.card.amount !== null && (!hasExactKeys(value.card.amount, ['amountMinor', 'currency', 'display'])
      || !Number.isSafeInteger(value.card.amount.amountMinor) || value.card.amount.amountMinor < 0
      || value.card.amount.currency !== 'CNY' || !isSafeText(value.card.amount.display, 40)))
    || (value.card.safetyHint !== null && !isSafeText(value.card.safetyHint, 160))
    || !value.detail || typeof value.detail !== 'object'
    || (!hasExactKeys(value.detail, ['headline', 'metrics', 'items', 'insights', 'suggestion'])
      && !hasExactKeys(value.detail, ['headline', 'metrics', 'items', 'insights', 'suggestion', 'callToAction']))
    || !isSafeText(value.detail.headline, 240)
    || !Array.isArray(value.detail.metrics) || value.detail.metrics.length > 8
    || value.detail.metrics.some(item => !item || !hasExactKeys(item, ['label', 'value'])
      || !isSafeText(item.label, 80) || !isSafeText(item.value, 160))
    || !Array.isArray(value.detail.items) || value.detail.items.length > 32
    || value.detail.items.some(item => !validateDetailItem(item))
    || !Array.isArray(value.detail.insights) || value.detail.insights.length > 8
    || value.detail.insights.some(item => !isSafeText(item, 240))
    || !isSafeText(value.detail.suggestion, 240)
    || (Object.hasOwn(value.detail, 'callToAction')
      && !validateCallToAction(value.detail.callToAction, value.primaryActionLabel))
    || !isSafeText(value.primaryActionLabel, 80)) {
    throw new TypeError('recommendation artifact is invalid');
  }
}
function validateDetailItem(item) {
  if (!item || typeof item !== 'object' || Array.isArray(item)
    || !/^[A-Za-z0-9_.:-]{1,160}$/.test(item.id || '')
    || !isSafeText(item.title, 120) || !isSafeText(item.subtitle, 120)
    || !item.amount || !hasExactKeys(item.amount, ['amountMinor', 'currency', 'display'])
    || !Number.isSafeInteger(item.amount.amountMinor) || item.amount.amountMinor < 0
    || item.amount.currency !== 'CNY' || !isSafeText(item.amount.display, 40)) return false;
  if (hasExactKeys(item, ['id', 'title', 'subtitle', 'amount', 'status'])) {
    return isSafeText(item.status, 40);
  }
  if (!hasExactKeys(item, ['id', 'type', 'title', 'subtitle', 'amount', 'availability', 'action'])
    || !['credit_card', 'loan', 'utility'].includes(item.type)
    || !item.availability || !hasExactKeys(item.availability, ['state', 'reason'])
    || !['available', 'needs_information', 'unavailable'].includes(item.availability.state)
    || (item.availability.reason !== null && !isSafeText(item.availability.reason, 120))) return false;
  if (item.availability.state === 'available' && item.availability.reason !== null) return false;
  if (item.action === null) return item.availability.state !== 'available';
  return hasExactKeys(item.action, ['id', 'kind', 'label', 'targetIntentId', 'activationPhrases'])
    && /^[A-Za-z0-9_.:-]{1,160}$/.test(item.action.id || '')
    && ['agent_plan', 'agent_consult'].includes(item.action.kind)
    && isSafeText(item.action.label, 40)
    && /^[A-Za-z0-9_.:-]{1,160}$/.test(item.action.targetIntentId || '')
    && Array.isArray(item.action.activationPhrases)
    && item.action.activationPhrases.length > 0 && item.action.activationPhrases.length <= 6
    && item.action.activationPhrases.every(phrase => isSafeText(phrase, 80));
}
function validateCallToAction(value, primaryActionLabel) {
  return value && typeof value === 'object' && !Array.isArray(value)
    && hasExactKeys(value, ['message', 'primaryLabel', 'activationPhrases', 'confirmationHint'])
    && isSafeText(value.message, 160)
    && isSafeText(value.primaryLabel, 80)
    && value.primaryLabel === primaryActionLabel
    && Array.isArray(value.activationPhrases)
    && value.activationPhrases.length > 0 && value.activationPhrases.length <= 8
    && value.activationPhrases.every(phrase => isSafeText(phrase, 80))
    && isSafeText(value.confirmationHint, 160);
}
function hasExactKeys(value, keys) {
  const actual = Object.keys(value);
  return actual.length === keys.length && actual.every(key => keys.includes(key));
}
function requireId(value, field) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_.:-]{1,160}$/.test(value)) {
    throw new TypeError(`${field} is invalid`);
  }
}
function requireTime(value) {
  if (typeof value !== 'string' || !Number.isFinite(Date.parse(value))) throw new TypeError('now is invalid');
  return new Date(value).toISOString();
}
function isSafeText(value, maximum) {
  return typeof value === 'string' && value.length > 0 && value.length <= maximum && !/[<>]/.test(value);
}
function deepFreeze(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
  Object.values(value).forEach(deepFreeze);
  return Object.freeze(value);
}

module.exports = { createRecommendationArtifactProvider };
