import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/login', component: () => import('./views/Login.vue') },
  {
    path: '/',
    component: () => import('./views/Layout.vue'),
    children: [
      { path: '', component: () => import('./views/Dashboard.vue') },
      { path: 'readings', component: () => import('./views/Readings.vue') },
      { path: 'runs', component: () => import('./views/Runs.vue') },
      { path: 'runs/:id', component: () => import('./views/RunDetail.vue') },
      { path: 'metrics', component: () => import('./views/Metrics.vue') },
      { path: 'replay', component: () => import('./views/Replay.vue') },
      { path: 'feeder-scenarios', component: () => import('./views/FeederScenarios.vue') },
      { path: 'feeder-scenarios/:id', component: () => import('./views/FeederScenarioDetail.vue') },
    ],
  },
]

const router = createRouter({ history: createWebHistory(), routes })
router.beforeEach((to) => {
  if (to.path !== '/login' && !localStorage.getItem('token')) return '/login'
})
export default router
