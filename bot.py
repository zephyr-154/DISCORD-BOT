import logging

from currency_feature import setup_currency_feature
from dinner_feature import setup_dinner_feature
from feature_menu import setup_menu_feature
from voice_tracker import create_bot, load_config
from weather_feature import register_weather_commands


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s:%(name)s: %(message)s",
    )
    config = load_config()
    bot = create_bot(config)
    register_weather_commands(bot)
    setup_menu_feature(bot)
    setup_currency_feature(bot)
    setup_dinner_feature(bot)
    bot.run(config.token)


if __name__ == "__main__":
    main()
 