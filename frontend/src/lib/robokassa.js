import { API_BASE_URL } from '../config';

const PAYMENT_API_URL = `${API_BASE_URL}/payment/create`;

export async function createPaymentUrl(orderId, amount) {
  const response = await fetch(PAYMENT_API_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      order_id: orderId,
      amount,
    }),
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    throw new Error(errorData?.detail || 'Не удалось создать ссылку на оплату');
  }

  const data = await response.json();
  if (!data.payment_url) {
    throw new Error('Ссылка на оплату не получена');
  }

  return data.payment_url;
}

export async function redirectToPayment(orderId, amount) {
  const paymentUrl = await createPaymentUrl(orderId, amount);
  window.location.href = paymentUrl;
}
