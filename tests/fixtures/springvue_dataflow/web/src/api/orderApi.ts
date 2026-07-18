import axios from 'axios'

export function createOrder(data: object) {
  return axios.post('/api/orders', data)
}
