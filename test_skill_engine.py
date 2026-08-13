import sys
from analytics_platform.database import Store
from analytics_platform.stakeholder import StakeholderService

def main():
    try:
        print("Initializing Store...")
        store = Store(":memory:")
        
        print("Initializing StakeholderService...")
        service = StakeholderService(store)
        
        print(f"Loaded skills: {service.skill_registry.meta}")
        print("Done.")
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
