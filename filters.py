"""One Euro filter: smooth when the hand is slow, responsive when it moves fast."""
import math


class OneEuro:
    def __init__(self, min_cutoff=1.2, beta=0.02, d_cutoff=1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self.reset()

    def reset(self):
        self.t = None
        self.x = 0.0
        self.dx = 0.0

    @staticmethod
    def _alpha(cutoff, dt):
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, x, t):
        if self.t is None:
            self.t, self.x, self.dx = t, x, 0.0
            return x
        dt = t - self.t
        if dt > 0.15:  # pause or freeze gap: restart filter baseline without velocity spike
            self.t, self.x, self.dx = t, x, 0.0
            return x
        dt = max(dt, 1e-3)
        raw_dx = (x - self.x) / dt
        a_d = self._alpha(self.d_cutoff, dt)
        self.dx = a_d * raw_dx + (1.0 - a_d) * self.dx
        cutoff = self.min_cutoff + self.beta * abs(self.dx)
        a = self._alpha(cutoff, dt)
        self.x = a * x + (1.0 - a) * self.x
        self.t = t
        return self.x


class OneEuroPoint:
    """Convenience wrapper for 2D filtering of landmarks or screen coordinates."""
    def __init__(self, min_cutoff=1.0, beta=0.02, d_cutoff=1.0):
        self.fx = OneEuro(min_cutoff, beta, d_cutoff)
        self.fy = OneEuro(min_cutoff, beta, d_cutoff)

    def reset(self):
        self.fx.reset()
        self.fy.reset()

    def __call__(self, x, y, t):
        return self.fx(x, t), self.fy(y, t)


class DeadzoneFilter:
    """
    Suppresses micro-jitter below a deadzone radius.
    Applies a smooth linear attenuation ramp so there is zero snapping or stickiness.
    """
    def __init__(self, deadzone=1.5):
        self.deadzone = float(deadzone)
        self.last_x = None
        self.last_y = None

    def reset(self):
        self.last_x = None
        self.last_y = None

    def __call__(self, x, y):
        if self.deadzone <= 0:
            self.last_x, self.last_y = x, y
            return x, y
        if self.last_x is None:
            self.last_x, self.last_y = x, y
            return x, y
        dx = x - self.last_x
        dy = y - self.last_y
        dist = math.hypot(dx, dy)
        if dist < self.deadzone:
            return self.last_x, self.last_y
        # Smooth ramp above deadzone:
        factor = (dist - self.deadzone) / dist
        self.last_x += dx * factor
        self.last_y += dy * factor
        return self.last_x, self.last_y

