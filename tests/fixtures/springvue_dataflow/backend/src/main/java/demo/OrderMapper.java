package demo;

@Mapper
interface OrderMapper {
    int insertOrder(@Param("request") CreateOrderRequest request);
}
