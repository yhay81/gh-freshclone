require "minitest/autorun"

class FreshcloneRubyBoundaryTest < Minitest::Test
  def test_offline_bundle_executes
    assert_equal 4, 2 + 2
    puts "Ruby Bundler fixture passed offline"
  end
end
