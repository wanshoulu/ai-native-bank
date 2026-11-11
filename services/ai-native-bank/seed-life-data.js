'use strict';

/**
 * 生活服务域种子数据（天气提示 + 商户优惠）。
 *
 * - 天气：7 天循环序列（按 UTC 日期取模：offset = floor(epochDays) % 7），覆盖晴/多云/雨
 *   的常见形态；mock 城市为上海。2026-09-17 落在 offset 0（雨）、9/19-20 周末落 offset 2-3
 *   （晴）——按此序列雨天出行提示与周末好天气卡都有可验收日期。
 *   寒潮类提示（最高温 < 10°C）在当前季节数据下自然不触发，属数据驱动的正常行为。
 * - 优惠：商户优惠活动，含临期（validTo 2026-09-16）与常态（validTo 2026-12-31）两种节奏，
 *   支撑"优惠临期提醒"与"美食优惠"两个场景；到期后优惠自然下线。
 */

const WEATHER_CITY = '上海';

// 7 天循环：真实日内按 (epochDays % 7) 取当日
const WEATHER_FORECAST = [
  { offset: 0, condition: 'rain', tempLow: 19, tempHigh: 24 },
  { offset: 1, condition: 'cloudy', tempLow: 20, tempHigh: 27 },
  { offset: 2, condition: 'clear', tempLow: 21, tempHigh: 29 },
  { offset: 3, condition: 'clear', tempLow: 22, tempHigh: 30 },
  { offset: 4, condition: 'cloudy', tempLow: 20, tempHigh: 26 },
  { offset: 5, condition: 'rain', tempLow: 18, tempHigh: 22 },
  { offset: 6, condition: 'cloudy', tempLow: 19, tempHigh: 25 },
];

const MERCHANT_OFFERS = [
  { id: 'offer_dining_001', merchant: '老正兴菜馆', category: 'dining', title: '本帮菜满100减20', discountText: '满100减20', validFrom: '2026-09-01', validTo: '2026-09-16' },
  { id: 'offer_movie_001', merchant: 'SFC上影影城', category: 'movie', title: '影票低至6折', discountText: '低至6折', validFrom: '2026-09-05', validTo: '2026-09-16' },
  { id: 'offer_coffee_001', merchant: '瑞幸咖啡', category: 'dining', title: '会员日买一送一', discountText: '每周三买一送一', validFrom: '2026-09-01', validTo: '2026-09-30' },
  { id: 'offer_fuel_001', merchant: '中石化加油站', category: 'car', title: '加油满200减15', discountText: '满200减15', validFrom: '2026-09-01', validTo: '2026-12-31' },
  { id: 'offer_market_001', merchant: '城市超市', category: 'shopping', title: '商超满200减30', discountText: '满200减30', validFrom: '2026-09-10', validTo: '2026-12-31' },
];

const LIFE_SEED = Object.freeze({
  city: WEATHER_CITY,
  weatherForecast: Object.freeze(WEATHER_FORECAST.map(item => Object.freeze(item))),
  merchantOffers: Object.freeze(MERCHANT_OFFERS.map(item => Object.freeze(item))),
});

module.exports = Object.freeze({ LIFE_SEED });
