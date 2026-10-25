import unittest

from routers.shows import _rounded_whole_star_rating


class DerivedSeriesRatingTests(unittest.TestCase):
    def test_average_rounds_to_nearest_whole_star_on_ten_point_scale(self) -> None:
        self.assertEqual(_rounded_whole_star_rating([8.0, 10.0]), 9.0)
        self.assertEqual(_rounded_whole_star_rating([6.0, 8.0]), 7.0)
        self.assertEqual(_rounded_whole_star_rating([4.0, 6.0]), 5.0)

    def test_ignores_unrated_episodes_and_empty_values(self) -> None:
        self.assertEqual(_rounded_whole_star_rating([8.0, None, 10.0]), 9.0)
        self.assertIsNone(_rounded_whole_star_rating([None, None]))

    def test_exact_half_star_rounds_up(self) -> None:
        self.assertEqual(_rounded_whole_star_rating([4.0, 5.0]), 5.0)

    def test_rating_is_clamped_to_supported_range(self) -> None:
        self.assertEqual(_rounded_whole_star_rating([0.0]), 1.0)
        self.assertEqual(_rounded_whole_star_rating([10.0]), 10.0)


if __name__ == "__main__":
    unittest.main()
