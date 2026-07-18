package demo;

@Service
@RequiredArgsConstructor
class OrderService {
    private final OrderMapper orderMapper;

    @Transactional
    public OrderResponse createOrder(CreateOrderRequest request) {
        orderMapper.insertOrder(request);
        return new OrderResponse();
    }
}
