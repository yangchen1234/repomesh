package sample.parcelflow;

import java.time.Duration;

public interface PaymentGateway {
    Authorization authorize(String orderId, long amountCents, Duration timeout);

    record Authorization(String providerReference, boolean approved) {}
}

class TimeoutPaymentGateway implements PaymentGateway {
    @Override
    public Authorization authorize(String orderId, long amountCents, Duration timeout) {
        if (timeout.isNegative() || timeout.isZero()) {
            throw new IllegalArgumentException("timeout must be positive");
        }
        return new Authorization("local-" + orderId, amountCents > 0);
    }
}
