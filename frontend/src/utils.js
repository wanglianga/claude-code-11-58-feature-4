export function fmtBand(b) {
  if (!b) return '-'
  if (b.v === null || b.v === undefined) return '未知'
  if (b.lo === b.hi || b.hi === null || b.hi === undefined) {
    return b.hi === null || b.hi === undefined ? `${b.v}` : `${b.v}`
  }
  return `${b.v} [${b.lo} ~ ${b.hi}]`
}

export function fmtVal(v, digits = 2) {
  if (v === null || v === undefined) return '未知'
  return Number(v).toFixed(digits).replace(/\.?0+$/, '')
}

export const QUALITY_TAG = { GOOD: 'success', ESTIMATED: 'warning', UNKNOWN: 'danger' }
export const COMPARABLE_TAG = { FULL: 'success', PARTIAL: 'warning', NONE: 'danger' }
export const CUT_LABEL = {
  START: '起始', STOP: '停车', TURNBACK: '折返', SECTION_EXIT: '出区段',
  CONSIST_CHANGE: '换编组', MULTIPLIER_CHANGE: '倍率切换', DEVICE_RESET: '设备重启', END: '结束',
}
export const SOURCE_LABEL = { ONBOARD: '车载侧', SUPPLY: '供电侧' }
export const Q_LABEL = {
  traction: '牵引', regen: '回馈', aux: '辅助', import: '受电', export: '返送',
}
export const METRIC_LABEL = {
  NET_KWH: '净能耗(kWh)', KWH_PER_KM: '单位里程能耗(kWh/km)',
  KWH_PER_PASSENGER: '单位载客能耗(kWh/人)', REGEN_RATIO: '回馈比例',
}
export const SCOPE_LABEL = { RUN: '运行', SECTION: '区段', SHIFT: '班次', VEHICLE_MODEL: '车型' }
export const METHOD_LABEL = {
  TRACE_REPLAY: '实测轨迹回放', ENVELOPE_MC: '功率包络抽样',
}
export const METHOD_TAG = { TRACE_REPLAY: 'primary', ENVELOPE_MC: 'warning' }
export const LEVEL_LABEL = {
  EXACT: '精确匹配', LOAD_FALLBACK: '载荷档降级', CONSIST_FALLBACK: '编组降级',
  SECTION_FALLBACK: '区段降级', NO_REFERENCE: '无参考轨迹(扩大区间)',
}
export const LEVEL_TAG = {
  EXACT: 'success', LOAD_FALLBACK: 'info', CONSIST_FALLBACK: 'warning',
  SECTION_FALLBACK: 'warning', NO_REFERENCE: 'danger',
}
