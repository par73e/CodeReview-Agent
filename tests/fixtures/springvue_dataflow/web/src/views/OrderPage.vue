<script setup lang="ts">
import { createOrder } from '../api/orderApi'

async function submit() {
  const response = await createOrder({ userId: 7, amount: 99 })
  return response.data.orderId
}
</script>
